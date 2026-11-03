"""Owned workflow commands and snapshots; mutations are reconciled asynchronously."""
import json
import uuid
from typing import Literal
from fastapi import HTTPException,Request
from fastapi.responses import FileResponse
from pydantic import Field,model_validator
from . import workflow_graph as graph,agent_store
from .api_execution import lane


class Start(graph.Contract):
    request_id:str=Field(pattern=r'^[a-f0-9]{32}$')
    confirmed:Literal[True]
    authorization:graph.Authorization
    parent_run_id:str|None=Field(default=None,pattern=r'^[a-f0-9]{32}$')


class Reuse(graph.Contract):
    request_id:str=Field(pattern=r'^[a-f0-9]{32}$')
    file:str=Field(min_length=1,max_length=500)
    revision:int=Field(ge=0)
    name:str=Field(min_length=1,max_length=100)


class Command(graph.Contract):
    request_id:str=Field(pattern=r'^[a-f0-9]{32}$')
    action:Literal['pause','resume','cancel','answer']
    payload:dict=Field(default_factory=dict)

    @model_validator(mode='after')
    def decision_contract(self):
        if self.action=='answer':self.payload=graph.DecisionAnswer.model_validate(self.payload).model_dump(exclude_none=True)
        return self


def public(store,run):
    value={k:run[k] for k in ('id','project_id','session_id','parent_run_id','scope_confirmed','goal','status','graph_version','sealed','authorization',
                             'authorization_version','replan_after','repair_context','created_at','updated_at','terminal_reason','delivery','delivered_at','input_sequence','input_barrier_sequence')}
    value['state']=value.pop('status')
    successors=store.query('SELECT id FROM runs WHERE parent_run_id=? ORDER BY created_at DESC LIMIT 1',(run['id'],))
    value['successor_run_id']=successors[0]['id'] if successors else None
    value['activity']=run['activity']
    value['tasks']=[{k:task[k] for k in ('id','type','spec','status','selected','phase','revision','authorization_version','repair_count','user_revision_count','attempts',
        'wait_reason','candidate_result','accepted_result','quality','error_code','error_detail','created_at','updated_at')}
        for task in run['tasks']]
    value['control_jobs']=[{key:job[key] for key in ('id','control_kind','status','phase','attempts','error_code','created_at','updated_at')} for job in run['control_jobs']]
    from .run_inputs import public as public_input
    value['inputs']=[public_input(row) for row in store.query('SELECT * FROM run_inputs WHERE run_id=? ORDER BY sequence',(run['id'],))]
    value['drain_set']=run['drain_set']
    value['dependencies']=run['dependencies']
    value['replacements']=store.query('SELECT old_task_id,new_task_id,graph_version,source FROM run_task_replacements WHERE run_id=? ORDER BY graph_version,old_task_id',(run['id'],))
    for target,source in zip(value['tasks'],run['tasks']):
        if source['profile']:
            target['agent_policy']={key:source['profile'][key] for key in ('id','version','harness','model_policy','output_schema','artifact_policy')}
            target['agent_policy']['digest']=graph.digest(source['profile'])
    value['planners']=store.query('SELECT id,base_graph_version,status,applied_graph_version,error_code,created_at,updated_at FROM planner_runs WHERE run_id=? ORDER BY created_at',(run['id'],))
    value['decisions']=store.query("SELECT id,kind,graph_version,authorization_version,snapshot_digest,request_json,status FROM workflow_decisions WHERE run_id=? AND status='pending'",(run['id'],))
    for item in value['decisions']:item['request']=json.loads(item.pop('request_json'))
    value['reviews']=store.query('SELECT id,purpose,subject_task,graph_version,status FROM workflow_reviews WHERE run_id=? ORDER BY created_at',(run['id'],))
    value['commands']=store.query('SELECT id,action,status,error_code,created_at,applied_at FROM workflow_commands WHERE run_id=? ORDER BY created_at DESC LIMIT 20',(run['id'],))
    value['diagnoses']=[{'task_id':row['subject_task'],'diagnosis':json.loads(row['decision_json'])} for row in
        store.query("SELECT subject_task,decision_json FROM workflow_reviews WHERE run_id=? AND purpose='diagnose' AND status='completed' ORDER BY created_at",(run['id'],))]
    value['external_cleanup']=store.query('SELECT state,phase,error FROM execution_external_cleanup WHERE task_id=?',(run['id'],))
    value['token_usage']=store.query('SELECT COALESCE(SUM(used_tokens),0) AS known,COALESCE(SUM(CASE WHEN used_tokens IS NULL THEN reserved_tokens ELSE 0 END),0) AS reserved_or_unknown FROM workflow_model_reservations WHERE run_id=?',(run['id'],))[0]
    committed=value['token_usage']['known']+value['token_usage']['reserved_or_unknown'];value['token_usage']['limit']=run['authorization']['model_token_budget']
    value['token_usage']['remaining']=max(0,value['token_usage']['limit']-committed)
    value['cleanup_pending']=sum(1 for item in value['external_cleanup'] if item['state']!='complete')+store.query("SELECT COUNT(*) AS n FROM execution_cleanup WHERE task_id=? AND state<>'complete'",(run['id'],))[0]['n']
    for artifact in value['delivery']:
        from urllib.parse import quote
        artifact['url']=f'api/projects/{run["project_id"]}/runs/{run["id"]}/files/{quote(artifact["file"],safe="")}'
    return value


def register(app,store,own_project):
    store.execute('''CREATE TABLE IF NOT EXISTS workflow_start_requests(
        id TEXT PRIMARY KEY,project_id TEXT NOT NULL,owner TEXT NOT NULL,digest TEXT NOT NULL,run_id TEXT NOT NULL)''')

    def own_run(project,identifier,request):
        own_project(project,request);run=graph.get(store,identifier)
        if not run or run['project_id']!=project:raise HTTPException(404,'工作流不存在或不属于当前项目。')
        request.state.workflow_observation={'task_id':run['id'],'run_id':run['id']}
        return run

    @app.get('/api/projects/{project_id}/runs/capabilities')
    def capabilities(project_id:str,request:Request):
        own_project(project_id,request)
        from .operations import catalog
        from .specialist_profiles import catalogue
        workers=store.query('''SELECT c.catalogue FROM execution_operation_catalog c JOIN execution_workers w ON w.id=c.worker
            WHERE w.draining=0 AND w.heartbeat>? ORDER BY w.heartbeat DESC LIMIT 1''',(store.now()-30,))
        if workers:operations=json.loads(workers[0]['catalogue'])['operations']
        else:
            try:operations=catalog()['operations']
            except PermissionError:operations=[]
        return {'operations':operations,'profiles':catalogue(),'workers_ready':bool(workers),
                'content_fingerprint':graph.content_fingerprint(store,project_id),
                'authorization_schema':graph.Authorization.model_json_schema()}

    @app.get('/api/projects/{project_id}/runs')
    def list_runs(project_id:str,request:Request):
        own_project(project_id,request)
        return {'runs':store.query('''SELECT id,session_id,parent_run_id,goal,status AS state,graph_version,sealed,created_at,updated_at
            FROM runs WHERE project_id=? ORDER BY created_at DESC LIMIT 50''',(project_id,))}

    @app.post('/api/projects/{project_id}/runs',status_code=202)
    @lane('write')
    def start(project_id:str,body:Start,request:Request):
        own_project(project_id,request);owner=request.state.owner
        raw=body.authorization.model_dump();identity=graph.digest({'authorization':raw,'parent_run_id':body.parent_run_id})
        try:
            with store.connect() as db:
                previous=db.execute('SELECT * FROM workflow_start_requests WHERE id=?',(body.request_id,)).fetchone()
                if previous:
                    if (previous['owner'],previous['project_id'],previous['digest'])!=(owner,project_id,identity):raise ValueError('请求标识已用于其他内容')
                    return public(store,graph.get(store,previous['run_id']))
                if db.execute("SELECT id FROM execution_subjects WHERE project=? AND status IN ('queued','running','waiting','paused','cancelling')",(project_id,)).fetchone():
                    raise ValueError('项目中已有活动任务，请先完成或取消')
                parent=own_run(project_id,body.parent_run_id,request) if body.parent_run_id else None
                identifier=uuid.uuid4().hex
                run=graph.create(store,project_id,identifier,raw,parent_run_id=parent['id'] if parent else None)
                request.state.workflow_observation={'run_id':run['id']}
                db.execute('INSERT INTO workflow_start_requests VALUES (?,?,?,?,?)',(body.request_id,project_id,owner,identity,run['id']))
                return public(store,run)
        except ValueError as exc:raise HTTPException(409,str(exc)) from exc

    @app.get('/api/projects/{project_id}/runs/{run_id}')
    def detail(project_id:str,run_id:str,request:Request):
        return public(store,own_run(project_id,run_id,request))

    @app.get('/api/projects/{project_id}/runs/{run_id}/files/{filename}')
    async def delivery_file(project_id:str,run_id:str,filename:str,request:Request):
        def resolve():
            run=own_run(project_id,run_id,request)
            item=next((item for item in run['delivery'] if item['file']==filename),None)
            if run['delivered_at'] is None or item is None:raise HTTPException(404,'文件不属于已发布成果。')
            return item
        item=await app.state.api_execution.run('read',resolve)
        location=await app.state.api_execution.run('storage',store.artifacts.materialize,project_id,item['artifact_id'])
        active=item['type'] in ('text/html','image/svg+xml','application/xhtml+xml','application/xml','text/xml')
        return FileResponse(location,media_type=item['type'],filename=item['name'],
            content_disposition_type='attachment' if active else 'inline',
            headers={'Content-Security-Policy':"sandbox; default-src 'none'"} if active else {})

    @app.get('/api/projects/{project_id}/runs/{run_id}/tasks/{task_id}/file')
    async def task_file(project_id:str,run_id:str,task_id:str,file:str,request:Request):
        from pathlib import Path
        import mimetypes
        def resolve():
            run=own_run(project_id,run_id,request)
            task=next((t for t in run['tasks'] if t['id']==task_id and not t.get('control_kind')),None)
            if not task or file not in task['candidate_result']+task['accepted_result']:raise HTTPException(404,'产物不属于当前步骤。')
            rows=store.query('SELECT manifest FROM execution_step_checkpoints WHERE workflow=? AND step=? AND revision=?',
                             (run['id'],task_id,task['revision']))
            record=json.loads(rows[0]['manifest']).get(file) if rows else None
            if not record:raise HTTPException(404,'产物尚未持久化。')
            return record
        record=await app.state.api_execution.run('read',resolve)
        location=await app.state.api_execution.run('storage',store.artifacts.materialize,project_id,record['artifact'])
        media_type=mimetypes.guess_type(file)[0] or 'application/octet-stream'
        active=media_type in ('text/html','image/svg+xml','application/xhtml+xml','application/xml','text/xml')
        return FileResponse(location,media_type=media_type,filename=Path(file).name,
                            content_disposition_type='attachment' if active else 'inline',
                            headers={'Content-Security-Policy':"sandbox; default-src 'none'"} if active else {})

    @app.post('/api/projects/{project_id}/runs/{run_id}/tasks/{task_id}/reuse',status_code=201)
    @lane('write')
    def reuse(project_id:str,run_id:str,task_id:str,body:Reuse,request:Request):
        from pathlib import PurePosixPath
        import uuid
        try:
            with store.connect() as db:
                run=own_run(project_id,run_id,request)
                previous=db.execute('SELECT * FROM workflow_asset_reuse WHERE id=?',(body.request_id,)).fetchone()
                if previous:
                    if (previous['project_id'],previous['run_id'],previous['task_id'],previous['revision'],previous['path'])!=(project_id,run['id'],task_id,body.revision,body.file):
                        raise ValueError('Reuse request identity changed')
                    return next(a for a in store.assets(project_id) if a['id']==previous['asset_id'])
                task=next((t for t in run['tasks'] if t['id']==task_id and not t.get('control_kind')),None)
                if not task or task['status']!='succeeded' or task['revision']!=body.revision or body.file not in task['accepted_result']:
                    raise ValueError('Only the current accepted task revision can be reused')
                if not task['quality'].get('technical_valid'):raise ValueError('Source has not passed technical acceptance')
                rows=db.execute('SELECT manifest FROM execution_step_checkpoints WHERE workflow=? AND step=? AND revision=?',(run['id'],task_id,body.revision)).fetchone()
                record=json.loads(rows['manifest']).get(body.file) if rows else None
                if not record or not record.get('artifact'):raise ValueError('Source lacks an immutable object')
                artifact=store.artifacts.record(project_id,record['artifact'])
                if artifact['sha256']!=record['sha256'] or artifact['size']!=record['size']:raise ValueError('Source hash changed')
                checked=next((f for f in task['quality'].get('files',[]) if f['name']==body.file),None)
                if not checked or checked.get('sha256')!=record['sha256']:raise ValueError('Source does not match reviewed bytes')
                if len(store.assets(project_id))>=50:raise ValueError('Project asset limit reached')
                asset_id=uuid.uuid4().hex;filename=asset_id+PurePosixPath(body.file).suffix
                metadata={k:v for k,v in checked.items() if k not in ('name','sha256')}
                if checked.get('video'):metadata['kind']='video'
                elif checked.get('audio_track_present') and not checked.get('video'):metadata['kind']='audio'
                if metadata.get('kind') not in ('image','video','audio','subtitle','json','file'):raise ValueError('Output type is not importable')
                if metadata.get('audio_tracks'):
                    metadata['audio_tracks']=[{'index':i,'codec':track.get('codec_name',''),
                        'channels':track.get('channels',0),'title':f'音轨 {i}'} for i,track in enumerate(metadata['audio_tracks'])]
                metadata['size']=record['size']
                metadata['provenance']={'run_id':run['id'],'task_id':task_id,'revision':body.revision,'sha256':record['sha256']}
                store.check_storage(project_id,record['size'])
                store.add_asset(project_id,asset_id,body.name,filename,metadata,artifact=artifact)
                db.execute('INSERT INTO workflow_asset_reuse VALUES (?,?,?,?,?,?,?,?,?,?)',
                           (body.request_id,project_id,run['id'],task_id,body.revision,body.file,artifact['id'],artifact['sha256'],asset_id,store.now()))
                graph.event(store,run,'accepted_asset_reused',{'task_id':task_id,'revision':body.revision})
                return next(a for a in store.assets(project_id) if a['id']==asset_id)
        except ValueError as exc:raise HTTPException(409,str(exc)) from exc

    from .run_inputs import Input,accept as accept_input

    @app.post('/api/projects/{project_id}/runs/{run_id}/inputs',status_code=202)
    @lane('critical')
    def input_message(project_id:str,run_id:str,body:Input,request:Request):
        own_run(project_id,run_id,request)
        if body.source!='user':raise HTTPException(422,'质量反馈必须绑定原决策。')
        try:return accept_input(store,run_id,request.state.owner,body.model_dump())
        except ValueError as exc:raise HTTPException(409,str(exc)) from exc

    @app.get('/api/projects/{project_id}/runs/{run_id}/events')
    def events(project_id:str,run_id:str,request:Request,after:int=0,limit:int=100):
        run=own_run(project_id,run_id,request)
        records=store.query('SELECT seq,kind,graph_version,payload_json,created_at FROM workflow_graph_events WHERE run_id=? AND seq>? ORDER BY seq LIMIT ?',
                            (run['id'],max(0,after),max(1,min(200,limit))))
        for record in records:record['payload']=json.loads(record.pop('payload_json'))
        return {'events':records,'cursor':records[-1]['seq'] if records else after}

    @app.post('/api/projects/{project_id}/runs/{run_id}/commands',status_code=202)
    @lane('critical')
    def command(project_id:str,run_id:str,body:Command,request:Request):
        run=own_run(project_id,run_id,request)
        try:return graph.command(store,run['id'],request.state.owner,body.action,body.payload,body.request_id)
        except ValueError as exc:raise HTTPException(409,str(exc)) from exc
