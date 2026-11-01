import json
import asyncio
import sqlite3
from .store import INTEGRITY_ERRORS
import time
import os
from pathlib import Path
from typing import Literal

from fastapi import HTTPException, Request
from fastapi.responses import FileResponse, StreamingResponse
from pydantic import Field

from . import agent_store, workflows
from .agent_stream import bus as agent_stream_bus
from .models import Model
from .media import thumbnail
from .image_canvas import CanvasContext,bind_context
from .control_intent import ControlIntentClassifier, ControlUnavailable, UnrecognizedControl
import subprocess


class AgentMessage(Model):
    message: str = Field(min_length=1, max_length=8000)
    mode: Literal['plan', 'execute'] = 'plan'
    plan_id: str | None = None
    plan_digest: str | None = Field(default=None,pattern=r'^[a-f0-9]{64}$')
    parent_task_id: str | None = Field(default=None,pattern=r'^[a-f0-9]{32}$')
    asset_ids: list[str] = Field(default_factory=list,max_length=50)
    canvas_context:CanvasContext|None=None


class ControlMessage(Model):
    action:Literal['status','cancel','resume','pause']='status'
    message:str=Field(default='',max_length=200)


class QualityAction(Model):
    step_id:str=Field(min_length=1,max_length=40)
    revision:int=Field(ge=0)
    action:Literal['approve','reject','repair']
    feedback:str=Field(default='',max_length=2000)
    replacement:dict|None=None
    confirm_external:bool=False


class RestoreVersion(Model):
    version:str=Field(pattern=r'^[a-f0-9]{32}$')


class RenderObservation(Model):
    generation:int=Field(ge=1)
    sequence:int=Field(ge=1)
    render_ms:float=Field(ge=0,le=60000,allow_inf_nan=False)


class ReviewRetry(Model):
    digest:str=Field(pattern=r'^[a-f0-9]{64}$')


def register(app, store, own_project, lock):
    classifier = ControlIntentClassifier()
    app.state.control_intents = classifier
    def owned_turn(project_id, turn_id, request):
        own_project(project_id, request)
        row = agent_store.turn(store, turn_id)
        if not row or row['project'] != project_id:
            raise HTTPException(404, '对话不存在或不属于当前用户。')
        return row

    def summary(row):
        labels={'queued':'排队中','running':'执行中','waiting':'后台处理中','paused':'已暂停','failed':'未完成','succeeded':'已完成','cancelled':'已停止','cancelling':'正在停止'}
        text='任务'+labels.get(row['status'],row['status'])+'。'
        workflow=row.get('workflow')
        if workflow:
            review=workflow.get('agent_review')
            if review:text+={'review':'正在进行 Agent 步骤验收。','diagnose':'正在诊断问题并准备修复。','final':'正在最终验收，尚未交付。'}.get(review['purpose'],'')
            done=sum(s['status']=='succeeded' for s in workflow['steps'])
            text+=f'已验收完成 {done}/{len(workflow["steps"])} 个步骤。'
            current=next((s for s in workflow['steps'] if s['status'] not in ('succeeded','cancelled')),None)
            if current:text+='当前步骤：'+current['name']+'。'
        execution=row.get('execution') or {}
        reasons={'deadline':'已达到任务总时限。','budget':'累计运行预算已用完。','security':'安全策略已停止本任务。','cancelled':'后续操作已停止。'}
        text+=reasons.get(execution.get('termination_reason'),'')
        cleanup=(execution.get('cleanup') or {}).get('state')
        if cleanup in ('pending','cleaning'):text+='资源正在回收。'
        elif cleanup=='quarantined':text+='资源回收异常，已隔离，尚未重新分配。'
        elif cleanup=='complete':text+='本次执行资源已回收。'
        external=execution.get('external_cleanup') or []
        if any(x['state'] in ('unknown','unconfirmed') for x in external):text+='上游是否停止尚未确认，上游可能继续运行。'
        elif any(x['state'] in ('pending','running') for x in external):text+='正在向上游确认停止状态。'
        return text

    @app.post('/api/projects/{project_id}/agent/{turn_id}/render-observation')
    def render_observation(project_id:str,turn_id:str,body:RenderObservation,request:Request):
        row=owned_turn(project_id,turn_id,request)
        if body.generation>row.get('text_generation',0):return {'recorded':False}
        with store.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            if db.execute('SELECT COUNT(*) n FROM agent_render_observations WHERE task_id=?',(turn_id,)).fetchone()['n']>=200:return {'recorded':False}
            inserted=db.execute('INSERT OR IGNORE INTO agent_render_observations VALUES (?,?,?,?,?)',(turn_id,body.generation,body.sequence,body.render_ms,store.now())).rowcount
        if not inserted:return {'recorded':False}
        from .telemetry import emit
        emit('stream.browser_render',task_id=turn_id,generation=body.generation,event_sequence=body.sequence,duration_seconds=body.render_ms/1000)
        return {'recorded':True}

    @app.post('/api/projects/{project_id}/agent/{turn_id}/review-retry')
    def retry_review(project_id:str,turn_id:str,body:ReviewRetry,request:Request):
        owned_turn(project_id,turn_id,request)
        from .plan_readiness import retry_review as retry
        try:retry(store,turn_id,body.digest)
        except ValueError as exc:raise HTTPException(409,str(exc)) from exc
        return agent_store.public(agent_store.turn(store,turn_id))

    @app.get('/api/projects/{project_id}/capabilities')
    def capabilities(project_id:str,request:Request):
        own_project(project_id,request)
        from .operations import catalog
        active=store.query('SELECT c.catalogue FROM execution_operation_catalog c JOIN execution_workers w ON w.id=c.worker WHERE w.draining=0 AND w.heartbeat>? ORDER BY w.heartbeat DESC LIMIT 1',(store.now()-20,))
        return json.loads(active[0]['catalogue']) if active else catalog()

    @app.get('/api/projects/{project_id}/agent/{turn_id}/status')
    def task_status(project_id:str,turn_id:str,request:Request):
        row=owned_turn(project_id,turn_id,request)
        return {'summary':summary(row),'turn':agent_store.public(row)}

    @app.post('/api/projects/{project_id}/agent/{turn_id}/control')
    def control(project_id:str,turn_id:str,body:ControlMessage,request:Request):
        row=owned_turn(project_id,turn_id,request)
        action=body.action
        if body.message:
            # No project/database lock is held while waiting for classification.
            try:
                from .task_usage import control_target
                with control_target(store,turn_id):action=classifier.resolve(body.message,row['status'])
            except UnrecognizedControl:
                raise HTTPException(409,'未识别到明确的任务控制操作，未改变任务。可以查询进度、暂停、继续或取消；修改需求请先停止当前任务后重新讨论。')
            except ControlUnavailable:
                raise HTTPException(503,'暂时无法识别这句话，未改变任务。请使用操作按钮，或输入“查进度”“暂停任务”“继续任务”“取消任务”。')
            if 'action' in body.model_fields_set and body.action!=action:
                raise HTTPException(409,'文字与指定操作不一致，未改变任务。请只发送一种明确操作。')
        if action!='status':
            with lock, store.connect() as db:
                db.execute('BEGIN IMMEDIATE')
                # The original target may finish while the model is answering. Never
                # redirect its command to a newer turn in the same project.
                row=owned_turn(project_id,turn_id,request)
                if row['status'] not in ('queued','running','waiting','paused'):
                    raise HTTPException(409,'本轮任务已结束或正在停止，未执行新的控制操作。')
                if action=='cancel':cancel(project_id,turn_id,request)
                elif action=='resume':resume(project_id,turn_id,request)
                elif action=='pause':
                    try:workflows.pause(store,turn_id)
                    except ValueError as exc:raise HTTPException(409,str(exc)) from exc
        row=owned_turn(project_id,turn_id,request)
        receipt={'status':'','pause':'已请求暂停后续步骤。','resume':'已请求继续未完成步骤。','cancel':'已请求停止任务。'}[action]
        return {'action':action,'summary':receipt+summary(row),'turn':agent_store.public(row)}

    @app.post('/api/projects/{project_id}/agent/{turn_id}/quality')
    def quality(project_id:str,turn_id:str,body:QualityAction,request:Request):
        owned_turn(project_id,turn_id,request)
        from .task_lifecycle import TaskLimitReached
        try:workflows.quality_action(store,turn_id,body.step_id,body.revision,body.action,request.state.owner,
                                     body.feedback,body.replacement,body.confirm_external)
        except (ValueError,TaskLimitReached) as exc:raise HTTPException(409,str(exc)) from exc
        return agent_store.public(agent_store.turn(store,turn_id))

    @app.post('/api/projects/{project_id}/agent/{turn_id}/decisions/{request_id}')
    def user_decision(project_id:str,turn_id:str,request_id:str,body:dict,request:Request):
        owned_turn(project_id,turn_id,request)
        from .user_decisions import answer
        try:answer(store,turn_id,request_id,body,request.state.owner)
        except ValueError as exc:raise HTTPException(409,str(exc)) from exc
        return agent_store.public(agent_store.turn(store,turn_id))

    @app.get('/api/projects/{project_id}/checkpoints')
    def project_versions(project_id:str,request:Request):
        own_project(project_id,request)
        from .checkpoints import versions
        return {'versions':versions(store,project_id)}

    @app.post('/api/projects/{project_id}/checkpoints/restore')
    def restore(project_id:str,body:RestoreVersion,request:Request):
        own_project(project_id,request)
        from .checkpoints import rollback
        try:return {'versions':rollback(store,project_id,body.version)}
        except ValueError as exc:raise HTTPException(409,str(exc)) from exc

    @app.get('/api/agent/health')
    def agent_health():
        from .task_leases import CLOCK
        workers = store.query("SELECT w.model,h.state,h.next_retry FROM execution_workers w LEFT JOIN execution_model_health h ON h.worker=w.id WHERE w.model<>'' AND w.draining=0 AND w.heartbeat>"+CLOCK+'-20')
        if workers:
            eligible=[w for w in workers if not w['next_retry'] or w['next_retry']<=store.now()]
            selected=(eligible or workers)[0]
            return {'available':bool(eligible), 'backend':'Codex App Server', 'model':selected['model'],
                    'model_health':selected['state'] or 'unverified','isolation':'attempt-filesystem-process-network-identity',
                    'message':'模型服务暂不可用，稍后自动重试' if not eligible else '智能体已就绪' if selected['state']=='healthy' else 'Agent 在线，模型连接待验证'}
        # Compatibility with pre-migration launchers; new workers never write a
        # shared health file, which would race between machines.
        path = store.root / 'agent-health.json'
        try:
            data = json.loads(path.read_text())
            fresh = time.time() - data['updated'] < 20
        except (OSError, ValueError, KeyError):
            fresh, data = False, {}
        return {'available': fresh and data.get('ready', False),
                'backend': 'Codex App Server', 'model': data.get('model', ''),
                'isolation': 'project-filesystem-process-network',
                'message': '智能体已就绪' if fresh and data.get('ready') else '智能体服务暂不可用'}

    @app.get('/api/projects/{project_id}/agent')
    def conversation(project_id: str, request: Request):
        own_project(project_id, request)
        rows = store.query('SELECT id FROM agent_turns WHERE project=? ORDER BY created', (project_id,))
        return {'turns': [agent_store.public(agent_store.turn(store, row['id'])) for row in rows]}

    @app.post('/api/projects/{project_id}/agent/messages', status_code=202)
    def send(project_id: str, body: AgentMessage, request: Request):
        with lock, store.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            own_project(project_id, request)
            try:store.admit_tasks(project_id)
            except ValueError as exc:raise HTTPException(429,str(exc)) from exc
            if not body.message.strip():
                raise HTTPException(422, '请输入剪辑需求。')
            if not agent_health()['available']:
                raise HTTPException(503, '智能体服务暂不可用，请稍后重试。')
            if store.query("SELECT id FROM jobs WHERE project=? AND status IN ('queued','running')", (project_id,)):
                raise HTTPException(409, '当前项目有处理任务，请等待完成后再让智能体操作。')
            if store.query("SELECT id FROM agent_turns WHERE project=? AND status IN ('queued','running','waiting','paused','cancelling')", (project_id,)):
                raise HTTPException(409, '智能体正在处理当前项目，可先停止本轮再发送新消息。')
            if len(store.query("SELECT id FROM agent_turns WHERE status IN ('queued','running','waiting')")) >= int(os.environ.get('VIDEO_USE_QUEUE_LIMIT', '100')):
                raise HTTPException(429, '智能体任务队列已满，请稍后再试。')
            owner = store.query('SELECT owner FROM projects WHERE id=?',(project_id,))[0]['owner']
            count = store.query("SELECT COUNT(*) AS n FROM agent_turns t JOIN projects p ON p.id=t.project WHERE p.owner=? AND t.status IN ('queued','running','waiting')",(owner,))[0]['n']
            if count >= int(os.environ.get('VIDEO_USE_USER_QUEUE_LIMIT', '4')):
                raise HTTPException(429, '你的待处理任务已达上限，请等待已有任务完成。')
            if body.parent_task_id:
                owned_turn(project_id,body.parent_task_id,request)
                if body.mode=='execute' and body.parent_task_id!=body.plan_id:
                    raise HTTPException(422,'执行任务只能关联已确认的方案')
            if body.mode == 'execute':
                from .plan_readiness import require
                try:require(store,project_id,body.plan_id,body.plan_digest)
                except ValueError as exc:raise HTTPException(409,str(exc)) from exc
                approved_plan=agent_store.turn(store,body.plan_id)
                if body.asset_ids and set(body.asset_ids)!={a['id'] for a in approved_plan.get('attachments',[])}:
                    raise HTTPException(409,'确认时选择的素材已变化，请重新讨论方案')
                inherited=approved_plan.get('canvas_context')
                value={k:inherited[k] for k in ('revision','layer_id')} if inherited else None
                if body.canvas_context and body.canvas_context.model_dump()!=value:
                    raise HTTPException(409,'执行时不能更换已确认的目标图层，请重新讨论方案')
            else:value=body.canvas_context.model_dump() if body.canvas_context else None
            context=bind_context(store,project_id,value)
            try:
                assets={a['id']:a for a in store.assets(project_id)}
                if not set(body.asset_ids).issubset(assets):raise HTTPException(422,'附加素材不属于当前项目。')
                attachments=[]
                for asset_id in dict.fromkeys(body.asset_ids):
                    a=assets[asset_id];kind=a['metadata']['kind'];is_video=kind=='video'
                    attachments.append({'id':asset_id,'name':a['name'],'type':'video/mp4' if is_video else a['metadata'].get('mime','text/plain'),
                        'url':f'api/projects/{project_id}/assets/{asset_id}/file',
                        'poster_url':f'api/projects/{project_id}/assets/{asset_id}/thumbnail' if kind in ('video','image') else None})
                turn_id = agent_store.add_turn(store, project_id, body.mode, body.message.strip(),attachments,context,body.plan_id if body.mode=='execute' else None,parent_task_id=body.parent_task_id)
            except INTEGRITY_ERRORS as exc:
                raise HTTPException(409, '当前项目已经有一轮对话正在处理。') from exc
            from .plan_readiness import register as register_route,consume
            register_route(store,turn_id,'heavy' if body.mode=='execute' else 'light')
            if body.mode=='execute':consume(store,project_id,body.plan_id,turn_id,body.plan_digest)
            return agent_store.public(agent_store.turn(store, turn_id))

    @app.post('/api/projects/{project_id}/agent/{turn_id}/cancel')
    def cancel(project_id: str, turn_id: str, request: Request):
        row = owned_turn(project_id, turn_id, request)
        if workflows.get(store, turn_id):
            return workflows.cancel(store, turn_id)
        from .task_lifecycle import cancel_task
        return cancel_task(store,turn_id)

    @app.post('/api/projects/{project_id}/agent/{turn_id}/resume')
    def resume(project_id: str, turn_id: str, request: Request):
        with lock:
            owned_turn(project_id, turn_id, request)
            from .task_lifecycle import TaskLimitReached
            try:workflows.resume(store, turn_id)
            except (ValueError,TaskLimitReached) as exc:raise HTTPException(409, str(exc)) from exc
            return agent_store.public(agent_store.turn(store, turn_id))

    @app.get('/api/projects/{project_id}/agent/{turn_id}/events')
    def events(project_id: str, turn_id: str, request: Request, after: int = 0):
        owned_turn(project_id, turn_id, request)
        rows = store.query('SELECT seq,kind,payload,created FROM agent_events WHERE turn_id=? AND seq>? ORDER BY seq LIMIT 200',
                           (turn_id, max(0, after)))
        return [{'seq':row['seq'], 'kind':row['kind'], 'payload':json.loads(row['payload']),'created':row['created']} for row in rows]

    @app.get('/api/projects/{project_id}/agent/{turn_id}/stream')
    def stream(project_id: str, turn_id: str, request: Request, after: int = 0):
        owned_turn(project_id,turn_id,request)
        header_cursor=request.headers.get('last-event-id','').strip()
        cursor=header_cursor or (str(max(0,after)) if after else '')

        def snapshot(position, with_events=True):
            row=agent_store.turn(store,turn_id)
            records=store.query('SELECT seq,kind,payload,created FROM agent_events WHERE turn_id=? AND seq>? ORDER BY seq LIMIT 200',(turn_id,position)) if with_events else []
            return row,records

        async def generate():
            redis_bus=agent_stream_bus()
            # Numeric cursors belong to the legacy database event log. Redis
            # Stream IDs contain a dash; this keeps old clients reconnecting
            # safely during a rolling upgrade.
            redis_mode=bool(redis_bus.url) and (not header_cursor or '-' in header_cursor)
            redis_position=cursor or '0-0'
            snapshot_cursor=''
            try:
                db_cursor=int(header_cursor) if header_cursor and '-' not in header_cursor else 0
            except ValueError:
                db_cursor=0
            db_position=max(0,after) if not header_cursor else max(0,db_cursor)
            initial=not header_cursor and not after
            signature=None;heartbeat=time.monotonic();last_snapshot=0.;row=None;records=[]
            while not await request.is_disconnected():
                if not redis_mode or row is None or time.monotonic()-last_snapshot>=1:
                    row,records=await asyncio.to_thread(snapshot,db_position,not redis_mode)
                    last_snapshot=time.monotonic()
                if not row:
                    yield 'event: gone\ndata: {}\n\n';return
                execution=row.get('execution') or {}
                current=(row['status'],row['updated'],len(row['text']),
                         int(execution.get('used_seconds',0)),json.dumps(execution.get('cleanup',{}),sort_keys=True))
                if current!=signature:
                    signature=current
                    payload=agent_store.public(row)
                    payload['stream_transport']='redis' if redis_mode else 'database'
                    if redis_mode and not initial and row['status'] in ('queued','running','waiting','cancelling'):
                        payload['text']=None
                        payload['_text_unchanged']=True
                    yield 'event: turn\ndata: '+json.dumps(payload,ensure_ascii=False)+'\n\n'
                if redis_mode:
                    if initial:
                        # The snapshot cursor is persisted with the text it
                        # contains. Replay older activity for a freshly opened
                        # page, while filtering text deltas already in it.
                        snapshot_cursor=row.get('stream_cursor') or ''
                        redis_position='0-0'
                        initial=False
                    live=await asyncio.to_thread(redis_bus.read,turn_id,redis_position,200,200)
                    if live is None:
                        redis_mode=False
                        db_position=0;signature=None;last_snapshot=0
                        continue
                    for record in live:
                        redis_position=record['id']
                        if snapshot_cursor and record['kind'] in ('text_delta','text_snapshot'):
                            try:
                                old=tuple(map(int,snapshot_cursor.split('-',1)))
                                current_id=tuple(map(int,record['id'].split('-',1)))
                            except (ValueError,TypeError):
                                old=current_id=()
                            if old and current_id and current_id<=old:
                                continue
                        if record['kind']=='terminal':last_snapshot=0
                        if record['kind']=='text_snapshot':
                            yield f"id: {redis_position}\nevent: text_snapshot\ndata: "+json.dumps(record['payload'],ensure_ascii=False)+'\n\n'
                        elif record['kind']=='text_delta':
                            yield f"id: {redis_position}\nevent: text\ndata: "+json.dumps(record['payload'],ensure_ascii=False)+'\n\n'
                        else:
                            event={'seq':redis_position,'kind':record['kind'],'payload':record['payload'],'created':int(redis_position.split('-',1)[0])/1000}
                            yield f"id: {redis_position}\nevent: activity\ndata: "+json.dumps(event,ensure_ascii=False)+'\n\n'
                    if row['status'] not in ('queued','running','waiting','cancelling') and not live:
                        yield 'event: done\ndata: '+json.dumps({'status':row['status']})+'\n\n';return
                    if time.monotonic()-heartbeat>12:
                        yield 'event: heartbeat\ndata: '+json.dumps({'time':time.time()})+'\n\n'
                        heartbeat=time.monotonic()
                    continue
                for record in records:
                    db_position=record['seq'];record['payload']=json.loads(record['payload'])
                    yield f'id: {db_position}\nevent: activity\ndata: '+json.dumps(record,ensure_ascii=False)+'\n\n'
                if row['status'] not in ('queued','running','waiting','cancelling') and len(records)<200:
                    yield 'event: done\ndata: '+json.dumps({'status':row['status']})+'\n\n';return
                if time.monotonic()-heartbeat>12:
                    yield 'event: heartbeat\ndata: '+json.dumps({'time':time.time()})+'\n\n'
                    heartbeat=time.monotonic()
                if redis_bus.url and redis_bus.enabled and not header_cursor.isdigit():
                    redis_mode=True;initial=True;signature=None;last_snapshot=0
                await asyncio.sleep(.5)
        return StreamingResponse(generate(),media_type='text/event-stream',headers={'Cache-Control':'no-cache','X-Accel-Buffering':'no'})

    @app.get('/api/projects/{project_id}/agent/{turn_id}/files/{name}')
    def artifact(project_id: str, turn_id: str, name: str, request: Request, download: bool = False):
        row = owned_turn(project_id, turn_id, request)
        record = next((a for a in row['artifacts'] if a['file'] == name), None)
        if not record:
            raise HTTPException(404, '结果文件不存在。')
        path = store.file(project_id, f'edit/agent_exports/{turn_id}/{name}')
        return FileResponse(path, filename=record['name'] if download else None,
                            media_type=record['type'])

    @app.get('/api/projects/{project_id}/agent/{turn_id}/files/{name}/thumbnail')
    def artifact_thumbnail(project_id: str, turn_id: str, name: str, request: Request):
        row=owned_turn(project_id,turn_id,request)
        record=next((a for a in row['artifacts'] if a['file']==name),None)
        if not record or not record['type'].startswith('video/'):
            raise HTTPException(404,'无视频封面。')
        source=store.file(project_id,f'edit/agent_exports/{turn_id}/{name}')
        try:path=thumbnail(source,store.project_dir(project_id)/'edit/cache/posters')
        except (ValueError,subprocess.TimeoutExpired) as exc:raise HTTPException(422,'视频封面暂不可用。') from exc
        return FileResponse(path,media_type='image/jpeg')
