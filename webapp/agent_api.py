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
from .control_intent import ControlIntentClassifier, ControlUnavailable, UnrecognizedControl,exact_action
import subprocess
from .api_execution import lane,APICapacityError


class AgentMessage(Model):
    message: str = Field(min_length=1, max_length=8000)
    mode: Literal['plan', 'execute'] = 'plan'
    plan_id: str | None = None
    plan_digest: str | None = Field(default=None,pattern=r'^[a-f0-9]{64}$')
    parent_task_id: str | None = Field(default=None,pattern=r'^[a-f0-9]{32}$')
    asset_ids: list[str] = Field(default_factory=list,max_length=50)
    canvas_context:CanvasContext|None=None


class ControlMessage(Model):
    action:Literal['status','cancel','resume','pause','replan']='status'
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


class AgentProfileChoice(Model):
    profile:str=Field(pattern=r'^[A-Za-z0-9_-]{1,64}$')


class HarnessSwitch(AgentProfileChoice):
    request_key:str=Field(min_length=1,max_length=100)
    expected_digest:str=Field(pattern=r'^[a-f0-9]{64}$')


class RenderObservation(Model):
    generation:int=Field(ge=1)
    sequence:int=Field(ge=1)
    render_ms:float=Field(ge=0,le=60000,allow_inf_nan=False)


class ReviewRetry(Model):
    digest:str=Field(pattern=r'^[a-f0-9]{64}$')


def register(app, store, own_project, lock):
    api_execution=app.state.api_execution
    classifier = ControlIntentClassifier()
    app.state.control_intents = classifier

    @app.get('/api/projects/{project_id}/agent/profiles')
    def agent_profiles(project_id:str,request:Request):
        own_project(project_id,request)
        from .agent_profiles import profiles,selected
        from .harness_adapters import get
        preferred=store.query('SELECT name FROM agent_project_profiles WHERE project=?',(project_id,))
        return {'selected':preferred[0]['name'] if preferred else selected().name,
                'profiles':[p.public()|{'available':bool(p.base_url and os.environ.get(p.credential_env) and get(p.harness).available()),
                                       'capabilities':get(p.harness).capabilities.__dict__} for p in profiles().values()]}

    @app.put('/api/projects/{project_id}/agent/profile')
    @lane('write')
    def choose_profile(project_id:str,body:AgentProfileChoice,request:Request):
        own_project(project_id,request)
        from .agent_profiles import selected
        from .harness_adapters import get
        try:
            profile=selected(body.profile);profile.credentials()
            if not get(profile.harness).available():raise ValueError('所选 Agent 运行时不可用。')
            with store.connect() as db:
                db.execute('BEGIN IMMEDIATE')
                if db.execute("SELECT id FROM agent_turns WHERE project=? AND status IN ('queued','running','waiting','paused','cancelling')",(project_id,)).fetchone():
                    raise ValueError('当前有活动任务，请使用该任务的切换入口。')
                db.execute('INSERT INTO agent_project_profiles VALUES (?,?) ON CONFLICT(project) DO UPDATE SET name=excluded.name',(project_id,profile.name))
        except ValueError as exc:raise HTTPException(409,str(exc)) from exc
        return profile.public()

    @app.post('/api/projects/{project_id}/agent/{run_id}/harness-switch',status_code=202)
    @lane('control')
    def switch_harness(project_id:str,run_id:str,body:HarnessSwitch,request:Request):
        row=owned_turn(project_id,run_id,request)
        if (row.get('workflow') or {}).get('dynamic'):
            raise HTTPException(409,'动态工作流使用已冻结的模型和 Agent 策略；请在新一轮授权前选择新的模型配置。')
        from .harness_switching import request as request_switch
        try:return request_switch(store,project_id,run_id,body.profile,body.request_key,body.expected_digest)
        except ValueError as exc:raise HTTPException(409,str(exc)) from exc

    @app.delete('/api/projects/{project_id}/agent/{run_id}/harness-switch/{switch_id}')
    @lane('control')
    def cancel_switch(project_id:str,run_id:str,switch_id:str,request:Request):
        owned_turn(project_id,run_id,request)
        from .harness_switching import cancel
        try:return cancel(store,project_id,run_id,switch_id)
        except ValueError as exc:raise HTTPException(409,str(exc)) from exc
    def owned_turn(project_id, run_id, request):
        own_project(project_id, request)
        row = agent_store.turn(store, run_id)
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

    @app.post('/api/projects/{project_id}/agent/{run_id}/render-observation')
    def render_observation(project_id:str,run_id:str,body:RenderObservation,request:Request):
        row=owned_turn(project_id,run_id,request)
        if body.generation>row.get('text_generation',0):return {'recorded':False}
        with store.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            if db.execute('SELECT COUNT(*) n FROM agent_render_observations WHERE task_id=?',(run_id,)).fetchone()['n']>=200:return {'recorded':False}
            inserted=db.execute('INSERT OR IGNORE INTO agent_render_observations VALUES (?,?,?,?,?)',(run_id,body.generation,body.sequence,body.render_ms,store.now())).rowcount
        if not inserted:return {'recorded':False}
        from .telemetry import emit
        emit('stream.browser_render',task_id=run_id,generation=body.generation,event_sequence=body.sequence,duration_seconds=body.render_ms/1000)
        return {'recorded':True}

    @app.post('/api/projects/{project_id}/agent/{run_id}/review-retry')
    @lane('control')
    def retry_review(project_id:str,run_id:str,body:ReviewRetry,request:Request):
        owned_turn(project_id,run_id,request)
        from .plan_readiness import retry_review as retry
        try:retry(store,run_id,body.digest)
        except ValueError as exc:raise HTTPException(409,str(exc)) from exc
        return agent_store.public(agent_store.turn(store,run_id))

    @app.get('/api/projects/{project_id}/capabilities')
    def capabilities(project_id:str,request:Request):
        own_project(project_id,request)
        from .operations import catalog
        active=store.query('SELECT c.catalogue FROM execution_operation_catalog c JOIN execution_workers w ON w.id=c.worker WHERE w.draining=0 AND w.heartbeat>? ORDER BY w.heartbeat DESC LIMIT 1',(store.now()-20,))
        return json.loads(active[0]['catalogue']) if active else catalog()

    @app.get('/api/projects/{project_id}/agent/{run_id}/status')
    @lane('critical')
    def task_status(project_id:str,run_id:str,request:Request):
        row=owned_turn(project_id,run_id,request)
        return {'summary':summary(row),'turn':agent_store.public(row)}

    @app.post('/api/projects/{project_id}/agent/{run_id}/control')
    async def control(project_id:str,run_id:str,body:ControlMessage,request:Request):
        row=await api_execution.run('critical',owned_turn,project_id,run_id,request)
        if (row.get('workflow') or {}).get('dynamic'):
            raise HTTPException(410,'请使用动态工作流的命令和授权入口。')
        action=body.action
        if body.message:
            # No project/database lock is held while waiting for classification.
            try:
                known=exact_action(body.message)
                if known:action=known
                else:
                    def classify_control():
                        from .task_usage import control_target
                        with control_target(store,run_id):return classifier.resolve(body.message,row['status'])
                    action=await api_execution.run('control',classify_control)
            except UnrecognizedControl:
                raise HTTPException(409,'未识别到明确的任务控制操作，未改变任务。可以查询进度、暂停、继续、取消，或明确说明要修改什么以重新规划。')
            except ControlUnavailable:
                raise HTTPException(503,'暂时无法识别这句话，未改变任务。请使用操作按钮，或输入“查进度”“暂停任务”“继续任务”“取消任务”“重新规划”。')
            if 'action' in body.model_fields_set and body.action!=action:
                raise HTTPException(409,'文字与指定操作不一致，未改变任务。请只发送一种明确操作。')
        def apply_control():
            if action=='status':return owned_turn(project_id,run_id,request)
            with lock, store.connect() as db:
                db.execute('BEGIN IMMEDIATE')
                # The original target may finish while the model is answering. Never
                # redirect its command to a newer turn in the same project.
                row=owned_turn(project_id,run_id,request)
                if row['status'] not in ('queued','running','waiting','paused'):
                    raise HTTPException(409,'本轮任务已结束或正在停止，未执行新的控制操作。')
                if action=='replan':
                    message=body.message.strip()
                    if not message:raise HTTPException(422,'重新规划需要说明要修改的内容。')
                    previous=agent_store.turn(store,run_id)
                    cancel(project_id,run_id,request)
                    # Cancellation and admission share this transaction. Rejection
                    # must roll back the old task as well as the new planning turn.
                    try:store.admit_tasks(project_id)
                    except ValueError as exc:raise HTTPException(429,str(exc)) from exc
                    if not agent_health()['available']:
                        raise HTTPException(503,'智能体服务暂不可用，原任务未改变。')
                    from .task_leases import daily_quota,quota_error
                    for category in ('planning_model','model'):
                        block=daily_quota(store,request.state.owner,category)
                        if block:
                            exc=quota_error(block)
                            raise HTTPException(429,detail={**exc.diagnostic,'message':str(exc)})
                    inherited=previous.get('canvas_context')
                    context=bind_context(store,project_id,
                        {k:inherited[k] for k in ('revision','layer_id')} if inherited else None)
                    from .plan_readiness import register as register_route,invalidate
                    invalidate(store,project_id,'用户已提出修改，需重新规划并确认')
                    new_id=agent_store.add_turn(store,project_id,'plan',message,
                        previous.get('attachments',[]),context,parent_task_id=run_id)
                    register_route(store,new_id,'light')
                    agent_store.event(store,run_id,'replan_requested',{'child_task_id':new_id,'message':message[:2000]})
                    agent_store.event(store,new_id,'progress',{'text':'已停止上一版方案，正在根据修改要求重新规划。'})
                    return {'old':agent_store.public(agent_store.turn(store,run_id)),
                            'new':agent_store.public(agent_store.turn(store,new_id)),
                            'replanned_from':run_id}
                if action=='cancel':cancel(project_id,run_id,request)
                elif action=='resume':resume(project_id,run_id,request)
                elif action=='pause':
                    try:workflows.pause(store,run_id)
                    except ValueError as exc:raise HTTPException(409,str(exc)) from exc
            return owned_turn(project_id,run_id,request)
        row=await api_execution.run('critical',apply_control)
        if action=='replan':
            return {'action':action,'summary':'已停止上一版方案，正在根据修改要求重新规划。','turn':row['new'],
                    'old_turn':row['old'],'replanned_from':row['replanned_from']}
        receipt={'status':'','pause':'已请求暂停后续步骤。','resume':'已请求继续未完成步骤。','cancel':'已请求停止任务。'}[action]
        return {'action':action,'summary':receipt+summary(row),'turn':agent_store.public(row)}

    @app.post('/api/projects/{project_id}/agent/{run_id}/quality')
    def quality(project_id:str,run_id:str,body:QualityAction,request:Request):
        owned_turn(project_id,run_id,request)
        from .task_lifecycle import TaskLimitReached
        try:workflows.quality_action(store,run_id,body.step_id,body.revision,body.action,request.state.owner,
                                     body.feedback,body.replacement,body.confirm_external)
        except (ValueError,TaskLimitReached) as exc:raise HTTPException(409,str(exc)) from exc
        return agent_store.public(agent_store.turn(store,run_id))

    @app.post('/api/projects/{project_id}/agent/{run_id}/decisions/{request_id}')
    def user_decision(project_id:str,run_id:str,request_id:str,body:dict,request:Request):
        owned_turn(project_id,run_id,request)
        from .user_decisions import answer
        try:answer(store,run_id,request_id,body,request.state.owner)
        except ValueError as exc:raise HTTPException(409,str(exc)) from exc
        return agent_store.public(agent_store.turn(store,run_id))

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
    @lane('health')
    def agent_health():
        from .task_leases import CLOCK
        workers = store.query("SELECT w.model,h.state,h.next_retry FROM execution_workers w LEFT JOIN execution_model_health h ON h.worker=w.id WHERE w.model<>'' AND w.draining=0 AND w.heartbeat>"+CLOCK+'-20')
        if workers:
            eligible=[w for w in workers if not w['next_retry'] or w['next_retry']<=store.now()]
            selected=(eligible or workers)[0]
            return {'available':bool(eligible), 'backend':'Agent Harness', 'model':selected['model'],
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
        if os.environ.get('VIDEO_USE_DYNAMIC_WORKFLOW')=='1':
            own_project(project_id,request)
            raise HTTPException(410,'当前部署使用动态工作流，请在执行范围确认后通过工作流入口开始。')
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
            if body.mode=='plan':
                from .plan_readiness import current
                from .task_leases import daily_quota,quota_error
                if not current(store,project_id):
                    for category in ('planning_model','model'):
                        block=daily_quota(store,owner,category)
                        if block:
                            exc=quota_error(block)
                            raise HTTPException(429,detail={**exc.diagnostic,'message':str(exc)})
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
                run_id = agent_store.add_turn(store, project_id, body.mode, body.message.strip(),attachments,context,body.plan_id if body.mode=='execute' else None,parent_task_id=body.parent_task_id)
            except INTEGRITY_ERRORS as exc:
                raise HTTPException(409, '当前项目已经有一轮对话正在处理。') from exc
            from .plan_readiness import register as register_route,consume
            register_route(store,run_id,'heavy' if body.mode=='execute' else 'light')
            if body.mode=='execute':consume(store,project_id,body.plan_id,run_id,body.plan_digest)
            return agent_store.public(agent_store.turn(store, run_id))

    @app.post('/api/projects/{project_id}/agent/{run_id}/cancel')
    @lane('critical')
    def cancel(project_id: str, run_id: str, request: Request):
        row = owned_turn(project_id, run_id, request)
        if workflows.get(store, run_id):
            return workflows.cancel(store, run_id)
        from .task_lifecycle import cancel_task
        return cancel_task(store,run_id)

    @app.post('/api/projects/{project_id}/agent/{run_id}/resume')
    @lane('critical')
    def resume(project_id: str, run_id: str, request: Request):
        with lock:
            owned_turn(project_id, run_id, request)
            from .task_lifecycle import TaskLimitReached
            try:workflows.resume(store, run_id)
            except (ValueError,TaskLimitReached) as exc:raise HTTPException(409, str(exc)) from exc
            return agent_store.public(agent_store.turn(store, run_id))

    @app.get('/api/projects/{project_id}/agent/{run_id}/events')
    def events(project_id: str, run_id: str, request: Request, after: int = 0):
        owned_turn(project_id, run_id, request)
        rows = store.query('SELECT seq,kind,payload,created FROM agent_events WHERE run_id=? AND seq>? ORDER BY seq LIMIT 200',
                           (run_id, max(0, after)))
        return [{'seq':row['seq'], 'kind':row['kind'], 'payload':json.loads(row['payload']),'created':row['created']} for row in rows]

    @app.get('/api/projects/{project_id}/agent/{run_id}/stream',response_class=StreamingResponse)
    async def stream(project_id: str, run_id: str, request: Request, after: int = 0):
        from .agent_stream import AsyncStreamReader
        permit=await api_execution.streams.acquire()
        try:
            def authorize():
                own_project(project_id,request)
                if not store.query('SELECT id FROM agent_turns WHERE id=? AND project=?',(run_id,project_id)):
                    raise HTTPException(404,'对话不存在或不属于当前用户。')
            await api_execution.run('auth',authorize)
        except BaseException:
            permit.release();raise
        redis_bus=AsyncStreamReader(agent_stream_bus())
        header_cursor=request.headers.get('last-event-id','').strip()
        cursor=header_cursor or (str(max(0,after)) if after else '')

        def snapshot(position, with_events=True):
            row=agent_store.turn(store,run_id)
            records=store.query('SELECT seq,kind,payload,created FROM agent_events WHERE run_id=? AND seq>? ORDER BY seq LIMIT 200',(run_id,position)) if with_events else []
            return row,records

        async def generate():
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
                    try:row,records=await api_execution.run('stream_read',snapshot,db_position,not redis_mode)
                    except APICapacityError:
                        yield 'retry: 2000\nevent: retry\ndata: {"code":"API_STREAM_BUSY"}\n\n'
                        return
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
                    live=await redis_bus.read(run_id,redis_position,200,200)
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
        class ManagedStream(StreamingResponse):
            async def __call__(self,*args,**kwargs):
                try:return await super().__call__(*args,**kwargs)
                finally:
                    import anyio
                    try:
                        with anyio.CancelScope(shield=True):await redis_bus.aclose()
                    finally:permit.release()
        return ManagedStream(generate(),media_type='text/event-stream',headers={'Cache-Control':'no-cache','X-Accel-Buffering':'no'})

    @app.get('/api/projects/{project_id}/agent/{run_id}/files/{name}')
    async def artifact(project_id: str, run_id: str, name: str, request: Request, download: bool = False):
        row=await api_execution.run('read',owned_turn,project_id,run_id,request)
        record = next((a for a in row['artifacts'] if a['file'] == name), None)
        if not record:
            raise HTTPException(404, '结果文件不存在。')
        path=await api_execution.run('storage',store.file,project_id,f'edit/agent_exports/{run_id}/{name}')
        active=record['type'] in ('text/html','image/svg+xml','application/xhtml+xml','application/xml','text/xml')
        return FileResponse(path, filename=record['name'] if download or active else None,
                            media_type=record['type'],headers={'Content-Security-Policy':"sandbox; default-src 'none'"} if active else {})

    @app.get('/api/projects/{project_id}/agent/{run_id}/files/{name}/thumbnail')
    async def artifact_thumbnail(project_id: str, run_id: str, name: str, request: Request):
        row=await api_execution.run('read',owned_turn,project_id,run_id,request)
        record=next((a for a in row['artifacts'] if a['file']==name),None)
        if not record or not record['type'].startswith('video/'):
            raise HTTPException(404,'无视频封面。')
        source=await api_execution.run('storage',store.file,project_id,f'edit/agent_exports/{run_id}/{name}')
        try:path=await api_execution.run('media',thumbnail,source,store.project_dir(project_id)/'edit/cache/posters')
        except (ValueError,subprocess.TimeoutExpired) as exc:raise HTTPException(422,'视频封面暂不可用。') from exc
        return FileResponse(path,media_type='image/jpeg')
