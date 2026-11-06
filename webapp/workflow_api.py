"""Owned workflow commands and snapshots; mutations are reconciled asynchronously."""
import asyncio
import json
import time
import uuid
from typing import Literal
from fastapi import HTTPException,Request
from fastapi.responses import FileResponse,StreamingResponse
from pydantic import Field,model_validator
from . import workflow_graph as graph,agent_store
from .api_execution import lane,APICapacityError


class ModelChoice(graph.Contract):
    profile:str=Field(pattern=r'^[A-Za-z0-9_-]{1,64}$')
    reasoning_effort:str=Field(min_length=1,max_length=16)
    profile_digest:str=Field(pattern=r'^[a-f0-9]{64}$')


class Start(graph.Contract):
    request_id:str=Field(pattern=r'^[a-f0-9]{32}$')
    confirmed:Literal[True]
    authorization:graph.Authorization
    parent_run_id:str|None=Field(default=None,pattern=r'^[a-f0-9]{32}$')
    model_choice:ModelChoice|None=None


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
    def execution_location(item):
        bindings=item.get('bindings') or {}
        backend=bindings.get('_backend') or bindings.get('backend')
        if isinstance(backend,dict):backend=backend.get('backend') or backend.get('runtime_kind')
        environment=bindings.get('_environment')
        if not backend and isinstance(environment,dict):backend=environment.get('backend') or environment.get('runtime_kind')
        if backend in ('opensandbox','agent_runtime','command_runtime'):return 'opensandbox'
        if backend in ('local_bwrap','bwrap'):return 'bwrap_sandbox'
        if item.get('control_kind') or item.get('type') in ('agent','script'):return 'bwrap_sandbox'
        if item.get('type')=='operation':
            try:
                from .operations import get
                operation=get(item['spec']['operation'],item['spec'].get('implementation'))
                return 'executor_host' if operation.external_side_effects else 'bwrap_sandbox'
            except (KeyError,ValueError):pass
        return 'executor_host'

    value={k:run[k] for k in ('id','project_id','session_id','parent_run_id','scope_confirmed','goal','status','graph_version','sealed','authorization',
                             'authorization_version','replan_after','repair_context','created_at','updated_at','terminal_reason','delivery','delivered_at','input_sequence','input_barrier_sequence')}
    value['state']=value.pop('status')
    from .model_selection import for_run
    value['model_configuration']=for_run(store,run['id'])
    successors=store.query('SELECT id FROM runs WHERE parent_run_id=? ORDER BY created_at DESC LIMIT 1',(run['id'],))
    value['successor_run_id']=successors[0]['id'] if successors else None
    value['activity']=run['activity']
    revision_versions={(row['task_id'],row['revision']):row['created_graph_version'] for row in
        store.query('SELECT task_id,revision,created_graph_version FROM workflow_task_revisions WHERE run_id=?',(run['id'],))}
    value['tasks']=[]
    for task in run['tasks']:
        public_task={k:task[k] for k in ('id','type','spec','status','selected','phase','revision','authorization_version','repair_count','user_revision_count','attempts',
            'wait_reason','candidate_result','accepted_result','quality','error_code','error_detail','created_at','updated_at')}
        public_task['revision_graph_version']=revision_versions.get((task['id'],task['revision']),task['created_graph_version'])
        public_task['execution_location']=execution_location(task)
        value['tasks'].append(public_task)
    value['control_jobs']=[]
    for job in run['control_jobs']:
        public_job={key:job[key] for key in ('id','control_kind','status','phase','attempts','error_code','created_graph_version','created_at','updated_at')}
        public_job['execution_location']=execution_location(job)
        value['control_jobs'].append(public_job)
    from .run_inputs import public as public_input
    value['inputs']=[public_input(row) for row in store.query('SELECT * FROM run_inputs WHERE run_id=? ORDER BY sequence',(run['id'],))]
    value['drain_set']=run['drain_set']
    value['dependencies']=run['dependencies']
    value['replacements']=store.query('SELECT old_task_id,new_task_id,graph_version,source FROM run_task_replacements WHERE run_id=? ORDER BY graph_version,old_task_id',(run['id'],))
    for target,source in zip(value['tasks'],run['tasks']):
        if source['profile']:
            target['agent_policy']={key:source['profile'][key] for key in ('id','version','harness','model_policy','output_schema','artifact_policy')}
            target['agent_policy']['digest']=graph.digest(source['profile'])
    value['planners']=store.query('SELECT id,task_id,base_graph_version,status,applied_graph_version,trigger_json,error_code,created_at,updated_at FROM planner_runs WHERE run_id=? ORDER BY created_at',(run['id'],))
    for planner in value['planners']:
        trigger=json.loads(planner.pop('trigger_json'))
        task_ids=[]
        if isinstance(trigger.get('completed'),list):task_ids.extend(item for item in trigger['completed'] if isinstance(item,str))
        if isinstance(trigger.get('task_id'),str) and trigger['task_id']:task_ids.append(trigger['task_id'])
        control_ids=[]
        for key in ('rejected','review_incomplete','failure_diagnosed','review_rejected','task_review_rejected'):
            if isinstance(trigger.get(key),str) and trigger[key]:control_ids.append(trigger[key])
        planner['trigger_task_ids']=list(dict.fromkeys(task_ids))
        planner['trigger_control_job_ids']=list(dict.fromkeys(control_ids))
    value['plan_drafts']=store.query('''SELECT planning_request_id,schema_version,status,revision,
        repeated_error_count,created_at,updated_at FROM workflow_plan_drafts WHERE run_id=? ORDER BY created_at''',(run['id'],))
    value['decisions']=store.query("SELECT id,kind,graph_version,authorization_version,snapshot_digest,request_json,status FROM workflow_decisions WHERE run_id=? AND status='pending'",(run['id'],))
    for item in value['decisions']:item['request']=json.loads(item.pop('request_json'))
    value['reviews']=store.query('SELECT id,task_id,purpose,subject_task,graph_version,status FROM workflow_reviews WHERE run_id=? ORDER BY created_at',(run['id'],))
    value['commands']=store.query('SELECT id,action,status,error_code,created_at,applied_at FROM workflow_commands WHERE run_id=? ORDER BY created_at DESC LIMIT 20',(run['id'],))
    value['diagnoses']=[{'task_id':row['subject_task'],'diagnosis':json.loads(row['decision_json'])} for row in
        store.query("SELECT subject_task,decision_json FROM workflow_reviews WHERE run_id=? AND purpose='diagnose' AND status='completed' ORDER BY created_at",(run['id'],))]
    value['external_cleanup']=store.query('SELECT state,phase,error FROM execution_external_cleanup WHERE task_id=?',(run['id'],))
    value['token_usage']=store.query('''SELECT
        COALESCE(SUM(input_settled_tokens),0) AS settled_input,
        COALESCE(SUM(output_settled_tokens),0) AS settled_output,
        COALESCE(SUM(CASE WHEN state='partially_settled_output_unknown' THEN output_reserved_tokens ELSE 0 END),0) AS unknown_output_reserved,
        COALESCE(SUM(CASE WHEN used_tokens IS NULL THEN reserved_tokens ELSE 0 END),0) AS fully_unknown_reserved,
        COALESCE(SUM(COALESCE(used_tokens,reserved_tokens)),0) AS committed
        FROM workflow_model_reservations WHERE run_id=?''',(run['id'],))[0]
    value['token_usage']['known']=value['token_usage']['settled_input']+value['token_usage']['settled_output']
    value['token_usage']['reserved_or_unknown']=value['token_usage']['unknown_output_reserved']+value['token_usage']['fully_unknown_reserved']
    committed=value['token_usage']['committed'];value['token_usage']['limit']=run['authorization']['model_token_budget']
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
        from .model_selection import options
        return {'operations':operations,'profiles':catalogue(),'workers_ready':bool(workers),
                'model_choices':options(store,project_id),
                'model_protocol_policy':'plan_draft_v1_strict_preferred',
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
        raw=body.authorization.model_dump();identity=graph.digest({'authorization':raw,'parent_run_id':body.parent_run_id,
            'model_choice':body.model_choice.model_dump() if body.model_choice else None})
        try:
            with store.connect() as db:
                previous=db.execute('SELECT * FROM workflow_start_requests WHERE id=?',(body.request_id,)).fetchone()
                if previous:
                    if (previous['owner'],previous['project_id'],previous['digest'])!=(owner,project_id,identity):raise ValueError('请求标识已用于其他内容')
                    return public(store,graph.get(store,previous['run_id']))
                if db.execute("SELECT id FROM execution_subjects WHERE project=? AND status IN ('queued','running','waiting','paused','cancelling')",(project_id,)).fetchone():
                    raise ValueError('项目中已有活动任务，请先完成或取消')
                parent=own_run(project_id,body.parent_run_id,request) if body.parent_run_id else None
                from .model_selection import resolve,resolve_roles
                configuration=resolve(body.model_choice) if body.model_choice else None
                roles=resolve_roles(configuration)
                identifier=uuid.uuid4().hex
                run=graph.create(store,project_id,identifier,raw,parent_run_id=parent['id'] if parent else None,
                                 model_configuration=configuration,role_model_configurations=roles)
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

    @app.get('/api/projects/{project_id}/runs/{run_id}/stream',response_class=StreamingResponse)
    async def stream(project_id:str,run_id:str,request:Request,after:int=0):
        from .agent_stream import AsyncStreamReader,bus as stream_bus
        from .public_messages import read as public_messages
        api_execution=app.state.api_execution
        permit=await api_execution.streams.acquire()
        try:await api_execution.run('auth',own_run,project_id,run_id,request)
        except BaseException:
            permit.release();raise
        reader=AsyncStreamReader(stream_bus());header=request.headers.get('last-event-id','').strip()
        try:db_position=max(0,int(header or after))
        except ValueError:db_position=max(0,after)
        public_kinds=('progress','work','tool','workflow_graph','note')

        def snapshot(cursor,with_events=True,with_text=True):
            rows=store.query('SELECT status,updated_at FROM runs WHERE id=?',(run_id,))
            marks=','.join('?' for _ in public_kinds)
            events=store.query(f'SELECT seq,kind,payload,created FROM agent_events WHERE run_id=? AND kind IN ({marks}) AND seq>? ORDER BY seq LIMIT 200',
                (run_id,*public_kinds,cursor)) if with_events else []
            return (rows[0] if rows else None),events,public_messages(store,run_id) if with_text else []

        def durable_text(messages):
            if not messages:return None
            latest=max(messages,key=lambda item:(item['generation'],item['sequence']))
            return {'text':'\n\n'.join(item['text'] for item in messages if item['text']),
                'text_version':[latest['generation'],latest['sequence']],
                'text_phase':'streaming' if any(item['status']=='streaming' for item in messages) else 'idle'}

        async def generate():
            nonlocal db_position
            redis_position=header if '-' in header else await reader.latest_id(run_id)
            redis_mode=redis_position is not None;redis_position=redis_position or '0-0'
            replay_events=not header or '-' not in header;initial=True;heartbeat=time.monotonic();last_snapshot=0.;status=''
            text_signature=None
            try:
                while not await request.is_disconnected():
                    if initial or not redis_mode or time.monotonic()-last_snapshot>=2:
                        try:run,records,messages=await api_execution.run('stream_read',snapshot,db_position,replay_events or not redis_mode,initial or not redis_mode)
                        except APICapacityError:
                            yield 'retry: 2000\nevent: retry\ndata: {"code":"API_STREAM_BUSY"}\n\n';return
                        if not run:yield 'event: gone\ndata: {}\n\n';return
                        status=run['status'];last_snapshot=time.monotonic();text=durable_text(messages)
                        signature=json.dumps(text,sort_keys=True) if text else None
                        if text and signature!=text_signature:
                            text_signature=signature;yield 'event: text_snapshot\ndata: '+json.dumps(text,ensure_ascii=False)+'\n\n'
                        for record in records:
                            db_position=record['seq'];record['payload']=json.loads(record['payload'])
                            event='agent_text' if record['kind']=='note' else 'activity'
                            yield f'id: {db_position}\nevent: {event}\ndata: '+json.dumps(record,ensure_ascii=False)+'\n\n'
                        initial=False;replay_events=False
                    if redis_mode:
                        live=await reader.read(run_id,redis_position,200,200)
                        if live is None:redis_mode=False;continue
                        for record in live:
                            redis_position=record['id'];kind=record['kind']
                            if kind=='text_snapshot':yield f'id: {redis_position}\nevent: text_snapshot\ndata: '+json.dumps(record['payload'],ensure_ascii=False)+'\n\n'
                            elif kind=='text_delta':yield f'id: {redis_position}\nevent: text\ndata: '+json.dumps(record['payload'],ensure_ascii=False)+'\n\n'
                            elif kind in public_kinds:
                                event='agent_text' if kind=='note' else 'activity';value={'seq':redis_position,'kind':kind,'payload':record['payload'],'created':int(redis_position.split('-',1)[0])/1000}
                                yield f'id: {redis_position}\nevent: {event}\ndata: '+json.dumps(value,ensure_ascii=False)+'\n\n'
                        if status in ('SUCCEEDED','FAILED','CANCELLED') and not live:
                            yield 'event: done\ndata: '+json.dumps({'status':status})+'\n\n';return
                    else:
                        if status in ('SUCCEEDED','FAILED','CANCELLED') and not records:
                            yield 'event: done\ndata: '+json.dumps({'status':status})+'\n\n';return
                        await asyncio.sleep(.5)
                    if time.monotonic()-heartbeat>12:
                        yield 'event: heartbeat\ndata: '+json.dumps({'time':time.time()})+'\n\n';heartbeat=time.monotonic()
            finally:await reader.aclose()

        class ManagedStream(StreamingResponse):
            async def __call__(self,*args,**kwargs):
                try:return await super().__call__(*args,**kwargs)
                finally:permit.release()
        return ManagedStream(generate(),media_type='text/event-stream',headers={'Cache-Control':'no-cache','X-Accel-Buffering':'no'})

    @app.post('/api/projects/{project_id}/runs/{run_id}/commands',status_code=202)
    @lane('critical')
    def command(project_id:str,run_id:str,body:Command,request:Request):
        run=own_run(project_id,run_id,request)
        try:return graph.command(store,run['id'],request.state.owner,body.action,body.payload,body.request_id)
        except ValueError as exc:raise HTTPException(409,str(exc)) from exc
