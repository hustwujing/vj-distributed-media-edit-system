"""Durable, scoped Agent reviews. Decisions are applied only after a checkpoint.

The reviewing Agent has read-only access to inputs/results and can prepare new
repair scripts in its private repair directory. It cannot submit providers or
change acceptance contracts. Scheduling and publication remain server-owned.
"""
import json
import os
import uuid
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class Evidence(BaseModel):
    model_config = ConfigDict(extra='forbid')
    step_id: str
    criterion: int | None = Field(default=None, ge=0)
    files: list[str] = Field(min_length=1, max_length=20)
    observation: str = Field(min_length=1, max_length=2000)
    coverage: str = Field(default='',max_length=2000)


class Decision(BaseModel):
    model_config = ConfigDict(extra='forbid')
    request_id: str
    action: Literal['pass', 'repair', 'needs_user', 'stop']
    reason: str = Field(min_length=1, max_length=4000)
    finding: Literal['conformant','nonconformant','inconclusive'] | None = None
    evidence: list[Evidence] = Field(default_factory=list, max_length=100)
    step_id: str | None = None
    replacement: dict | None = None
    user_request: dict | None = None
    deliveries: list[str] = Field(default_factory=list, max_length=40)


def dispatch(store,turn_id,body,mode,review_id=None):
    """Shared by the launcher and the actual unprivileged HTTP gateway."""
    from . import workflows
    action=body['action']
    if action=='intent':
        from .execution_intent import declare
        return declare(store,turn_id,body.get('intent'))
    if action=='capabilities':
        from .operations import catalog
        return catalog()
    if action=='preflight':
        if mode!='execute' or review_id:raise ValueError('正式脚本预检查需要确认执行阶段')
        from .task_leases import CURRENT
        from .workflow_runtime import Adapter,StepFailure
        lease=CURRENT.get()
        if not lease or not lease.runtime:raise ValueError('预检查需要当前隔离执行环境')
        plan=workflows.validate_plan(body.get('plan'))
        project=store.query('SELECT project FROM agent_turns WHERE id=?',(turn_id,))[0]['project']
        adapter=Adapter(store,'',Path('/run/video-use-agent'))
        reports={}
        for step in plan.steps:
            if step.kind=='script':
                from . import agent_store
                agent_store.event(store,turn_id,'progress',{'text':'正在预检查：'+step.name})
                try:reports[step.id]=adapter.preflight({'id':'preflight','project':project,'turn_id':turn_id},{'id':step.id,'spec':step.model_dump(),'bindings':{}})
                except StepFailure as exc:reports[step.id]={'passed':False,**exc.diagnostic}
        return {'passed':all(r.get('passed') for r in reports.values()),'reports':reports}
    if action=='validate':return {'valid':True,'plan':workflows.validate_plan(body.get('plan')).model_dump()}
    if action=='decision':
        if not review_id or body.get('decision',{}).get('request_id')!=review_id:
            raise ValueError('当前执行器没有此验收请求的提交权限')
        return stage(store,turn_id,body['decision'])
    if action=='submit':
        if mode!='execute' or review_id:raise ValueError('只有已确认的普通执行轮可以提交新工作流')
        return workflows.submit(store,turn_id,body.get('plan'))
    if action!='status':raise ValueError('未知工作流操作')
    public=workflows.public(workflows.get(store,turn_id))
    return {**public,'review_context':context(store,turn_id)} if public else None


def current(store, wid):
    rows = store.query("SELECT * FROM workflow_agent_reviews WHERE workflow=? AND status IN ('pending','running','staged') ORDER BY created DESC LIMIT 1", (wid,))
    if not rows:
        return None
    row = rows[0]
    row['snapshot'] = json.loads(row['snapshot'])
    row['decision'] = json.loads(row['decision'])
    return row


def snapshot(w):
    return {s['id']: {'revision': s['revision'], 'files': s['result'] or s['candidate'],
                     'inputs': s['quality'].get('input_versions', {})} for s in w['steps']}


def queue(store, w, step=None, purpose='final'):
    from . import workflows
    with store.connect() as db:
        db.execute('BEGIN IMMEDIATE')
        from .task_lifecycle import check
        check(db,w['turn_id'])
        live = workflows.get(store, w['turn_id'])
        if live['status'] in ('cancelled','cancelling','succeeded'):
            return False
        if current(store, w['id']):
            return True
        count = db.execute('SELECT COUNT(*) AS n FROM workflow_agent_reviews WHERE workflow=?', (w['id'],)).fetchone()['n']
        if count >= int(os.environ.get('VIDEO_USE_MAX_AGENT_REVIEWS', '24')):
            workflows.state(store,w['turn_id'],'failed',error='自动验收与诊断次数已用完，请检查已有结果。')
            if step:workflows.change(store,w['id'],step['id'],status='paused',error_code='review_limit')
            return False
        rid = uuid.uuid4().hex
        db.execute('INSERT INTO workflow_agent_reviews(id,workflow,step,purpose,status,snapshot,created,updated) VALUES (?,?,?,?,?,?,?,?)',
                   (rid,w['id'],step['id'] if step else None,purpose,'pending',json.dumps(snapshot(live)),store.now(),store.now()))
        if step:
            workflows.change(store,w['id'],step['id'],status='diagnosing' if purpose=='diagnose' else 'agent_review')
        workflows.state(store,w['turn_id'],'resuming' if purpose=='final' else 'reviewing',
                        text={'final':'后台步骤已完成，正在进行最终验收。','review':'正在由 Agent 验收步骤内容。','diagnose':'正在由 Agent 分析步骤问题。'}[purpose])
    return True


def begin(store, turn_id):
    from . import workflows
    from .task_lifecycle import check
    with store.connect() as db:
        db.execute('BEGIN IMMEDIATE')
        check(db,turn_id)
        w=workflows.get(store,turn_id)
        if not w or w['status'] not in ('reviewing','resuming'):return None
        request=current(store,w['id'])
        if not request:
            if w['status']!='resuming' or any(s['status']!='succeeded' for s in w['steps']):
                raise ValueError('缺少验收请求，不能继续')
            queue(store,w)
            request=current(store,w['id'])
        if not request:return None
        if request['status']=='pending':
            # Parallel siblings may have committed after this review was queued.
            # The root Agent lease excludes all step leases; freeze the complete
            # snapshot only once the reviewer actually starts.
            db.execute('UPDATE workflow_agent_reviews SET snapshot=? WHERE id=?',
                       (json.dumps(snapshot(w)),request['id']))
        if request['runs']>=3:
            db.execute("UPDATE workflow_agent_reviews SET status='exhausted' WHERE id=?",(request['id'],))
            workflows.state(store,turn_id,'failed',error='本次 Agent 验收多次中断，请重新规划。')
            return None
        db.execute("UPDATE workflow_agent_reviews SET status='running',decision='{}',runs=runs+1,updated=? WHERE id=?",(store.now(),request['id']))
        db.execute("UPDATE agent_turns SET status='running' WHERE id=? AND status='queued'",(turn_id,))
        return current(store,w['id'])


def repairable_steps(w,request):
    """A failed result may originate in its dependency ancestors, not siblings."""
    steps={s['id']:s for s in w['steps']}
    if request['step'] is None:return set(steps) if request['purpose']=='final' else set()
    allowed=set();pending=[request['step']]
    while pending:
        sid=pending.pop()
        if sid in allowed or sid not in steps:continue
        allowed.add(sid);pending.extend(steps[sid]['spec']['depends_on'])
    return allowed


def context(store, turn_id):
    from . import workflows
    w=workflows.get(store,turn_id)
    request=current(store,w['id']) if w else None
    if not request:return None
    return {'request_id':request['id'],'purpose':request['purpose'],'step_id':request['step'],
            'snapshot':request['snapshot'],'repairable_steps':sorted(repairable_steps(w,request)),
            'steps':[{k:s[k] for k in ('id','spec','status','revision','result','candidate','quality','diagnostics','error','error_code')} for s in w['steps']]}


def validate_decision(w, request, raw):
    from .workflows import Step
    decision=Decision.model_validate(raw)
    if decision.finding is None:decision.finding='conformant' if decision.action=='pass' else 'nonconformant' if decision.action=='repair' else 'inconclusive'
    if decision.action=='pass' and decision.finding!='conformant':raise ValueError('不合格或无法判断的结果不能放行')
    if not decision.reason.strip():raise ValueError('请提供具体结论原因')
    if decision.request_id!=request['id'] or snapshot(w)!=request['snapshot']:
        raise ValueError('验收请求或输入版本已变化，请重新读取状态')
    steps={s['id']:s for s in w['steps']}
    for evidence in decision.evidence:
        s=steps.get(evidence.step_id)
        if not s or not set(evidence.files).issubset(set(s['candidate']+s['result'])) or not evidence.observation.strip():
            raise ValueError('验收证据必须引用该步骤实际产物并说明观察结果')
        if evidence.criterion is not None and evidence.criterion>=len(s['spec'].get('acceptance',[])):
            raise ValueError('验收条件索引不存在')
    if decision.action=='pass':
        if decision.replacement or decision.step_id:raise ValueError('通过结论不能同时修改步骤')
        targets=list(steps.values()) if request['purpose']=='final' else [steps[request['step']]]
        for s in targets:
            expected_inputs={x['id']:x['revision'] for x in w['steps'] if x['id'] in s['spec']['depends_on']}
            if s['quality'].get('input_versions',{})!=expected_inputs:raise ValueError('依赖版本已变化，必须重新执行和验收')
            if not s['quality'].get('technical_valid') or not s['quality'].get('passed'):
                raise ValueError('技术验收未通过，Agent 不能放行')
            if request['purpose']=='final' and s['status']!='succeeded':raise ValueError('仍有未完成步骤')
            proof=[e for e in decision.evidence if e.step_id==s['id']]
            if not proof:raise ValueError('通过结论必须逐步提供实际产物证据')
            video_files={f['name'] for f in s['quality'].get('files',[]) if f.get('video')}
            if any(set(e.files)&video_files and not e.coverage.strip() for e in proof):raise ValueError('视频验收必须说明抽帧或片段检查范围')
            for index,rule in enumerate(s['spec'].get('acceptance',[])):
                if rule['kind']=='agent' and not any(e.criterion==index and
                    (rule['file']=='*' or any(Path(f).name==rule['file'] for f in e.files)) for e in proof):
                    raise ValueError('请逐项提供 Agent 验收条件的证据')
        if request['purpose']=='final':
            available={f for s in targets for f in s['result']}
            if not decision.deliveries or not set(decision.deliveries).issubset(available):
                raise ValueError('最终验收必须选择已通过步骤验收的交付文件')
        elif decision.deliveries:raise ValueError('步骤验收不能标记最终交付')
    elif decision.action=='repair':
        sid=decision.step_id or request['step']
        s=steps.get(sid)
        if not s or sid not in repairable_steps(w,request):raise ValueError('只能修复当前步骤或其依赖祖先，不能修改无关分支')
        spec=Step.model_validate(decision.replacement).model_dump()
        # Implementation changes may not weaken intent, acceptance or permissions.
        original=Step.model_validate(s['spec']).model_dump()
        from .operations import for_step
        operation=for_step(spec)
        allowed={'argv'} if spec['kind']=='script' else {'parameters'} if operation and operation.retry_safe else set()
        if not allowed:raise ValueError('重新调用外部服务需要用户确认，请返回 needs_user')
        if any(spec[k]!=original[k] for k in original if k not in allowed):
            raise ValueError('自动修复只能调整本地命令或受控本地参数，不能改变验收标准、依赖、输入或执行策略')
        affected={sid}
        for child in w['steps']:
            if set(child['spec']['depends_on'])&affected:affected.add(child['id'])
        from .operations import for_step
        if any(for_step(child['spec']) and not for_step(child['spec']).retry_safe and (child['attempts'] or child['external_id'] or child['result']) for child in w['steps'] if child['id'] in affected):
            raise ValueError('修复会影响外部图片步骤，请返回 needs_user')
        if s['repair_count']>=int(os.environ.get('VIDEO_USE_MAX_REPAIRS','2')):
            raise ValueError('目标步骤修复次数已用完，请返回 stop')
        decision.step_id=sid
    else:
        if decision.deliveries:raise ValueError('暂停结论不能附带交付操作')
        if decision.user_request:
            from .user_decisions import UserRequest
            request_spec=UserRequest.model_validate(decision.user_request)
            if decision.action!='needs_user':raise ValueError('只有 needs_user 可请求用户决策')
            if decision.replacement and request_spec.kind!='external_call':raise ValueError('只有外部调用授权可附带重生成提案')
            if request_spec.kind=='external_call':
                if not decision.replacement:raise ValueError('外部调用授权必须提供明确提案')
                proposed=Step.model_validate(decision.replacement).model_dump()
                target=steps.get(decision.step_id or request['step'])
                if not target:raise ValueError('外部调用提案需要明确目标步骤')
                original=Step.model_validate(target['spec']).model_dump()
                if any(proposed[k]!=original[k] for k in original if k not in ('parameters','argv')):
                    raise ValueError('外部调用提案不能改变原验收、输入、依赖或权限')
        elif decision.replacement:raise ValueError('修复提案需要明确 external_call 用户授权请求')
        if decision.step_id and decision.step_id not in repairable_steps(w,request):
            raise ValueError('需要用户处理的目标步骤不存在或不属于当前验收')
    return decision


def stage(store, turn_id, raw):
    from . import workflows
    from .task_lifecycle import check
    with store.connect() as db:
        db.execute('BEGIN IMMEDIATE')
        check(db,turn_id)
        w=workflows.get(store,turn_id)
        request=current(store,w['id']) if w else None
        if not request or request['status'] not in ('running','staged') or w['status'] not in ('reviewing','resuming'):
            raise ValueError('当前没有正在执行的 Agent 验收')
        decision=validate_decision(w,request,raw)
        payload=decision.model_dump_json()
        if request['status']=='staged' and decision.model_dump()!=request['decision']:
            raise ValueError('本次验收已提交结论，不能替换')
        db.execute("UPDATE workflow_agent_reviews SET status='staged',decision=?,updated=? WHERE id=?",(payload,store.now(),request['id']))
    return {'accepted':True,'request_id':request['id'],'message':'结论已保存，结束本轮后由服务端校验并应用；尚未完成交付。'}


def commit(store, turn_id, adapter):
    """Called after the review sandbox stopped and its new scripts checkpointed."""
    from . import workflows,agent_store
    from .task_lifecycle import check
    w=workflows.get(store,turn_id)
    request=current(store,w['id']) if w else None
    if not request or request['status']!='staged':
        if w and w['status'] in ('reviewing','resuming'):
            workflows.state(store,turn_id,'paused',error='Agent 未提交结构化验收结论，成果保留，可继续检查。')
        return False
    decision=validate_decision(w,request,request['decision'])
    # Recheck real bytes before approving. The model cannot attest away a failing
    # technical condition or approve bytes different from those it reviewed.
    if decision.action=='pass':
        targets=w['steps'] if request['purpose']=='final' else [s for s in w['steps'] if s['id']==request['step']]
        for s in targets:
            report=adapter.validate(w,s,s['result'] or s['candidate'])
            old={f['name']:f.get('sha256') for f in s['quality'].get('files',[])}
            new={f['name']:f.get('sha256') for f in report.get('files',[])}
            if not report['technical_valid'] or not report['passed'] or old!=new:
                raise ValueError('产物已变化或技术复验失败，请重新检查')
    with store.connect() as db:
        db.execute('BEGIN IMMEDIATE')
        check(db,turn_id)
        w=workflows.get(store,turn_id)
        live=current(store,w['id'])
        if w['status'] not in ('reviewing','resuming') or not live or live['id']!=request['id']:
            raise ValueError('任务已停止或验收已失效')
        decision=validate_decision(w,live,decision.model_dump())
        db.execute("UPDATE workflow_agent_reviews SET status='applied',updated=? WHERE id=?",(store.now(),request['id']))
        s=next((s for s in w['steps'] if s['id']==request['step']),None)
        if decision.action=='pass' and request['purpose']=='final':
            row=agent_store.turn(store,turn_id)
            delivered={str(Path(f).relative_to('edit')) for f in decision.deliveries}
            primary=str(Path(decision.deliveries[0]).relative_to('edit'))
            artifacts=[dict(a,workflow_delivery=a['name'] in delivered,is_primary=a['name']==primary,
                            role='deliverable' if a['name'] in delivered else 'intermediate') for a in row['artifacts']]
            db.execute('UPDATE agent_turns SET artifacts=? WHERE id=?',(json.dumps(artifacts),turn_id))
            workflows.state(store,turn_id,'succeeded',text='最终验收通过，已完成交付。\n\n'+decision.reason)
        elif decision.action=='pass':
            report={**s['quality'],'requires_agent':False,'agent_review':decision.model_dump(),
                    'checks':[dict(c,passed=True) if c.get('kind')=='agent' else c for c in s['quality'].get('checks',[])]}
            human=report.get('requires_review',False)
            workflows.change(store,w['id'],s['id'],quality=report,status='awaiting_review' if human else 'succeeded',
                             result=[] if human else s['candidate'],error_code='quality' if human else '',
                             error='请确认此步骤效果。' if human else '')
            workflows.state(store,turn_id,'paused' if human else 'ready',error='此步骤需要用户确认。' if human else '')
        elif decision.action=='repair':
            # Reuse version invalidation, budget and input-contract checks. The
            # active lease belongs to this server commit, not a concurrent user.
            workflows.state(store,turn_id,'paused')
            workflows.quality_action(store,turn_id,decision.step_id,
                next(x['revision'] for x in w['steps'] if x['id']==decision.step_id),'repair','agent',
                feedback=decision.reason,replacement=decision.replacement,_agent_commit=True)
        else:
            if s is None:
                s=next((item for item in w['steps'] if item['id']==decision.step_id),w['steps'][-1])
            if s:
                human=decision.action=='needs_user'
                names={str(Path(f).relative_to('edit')) for f in s['result']+s['candidate']}
                row=agent_store.turn(store,turn_id)
                artifacts=[dict(a,quality_state='pending',purpose='quality_candidate') if a['name'] in names else a for a in row['artifacts']]
                db.execute('UPDATE agent_turns SET artifacts=? WHERE id=?',(json.dumps(artifacts),turn_id))
                workflows.change(store,w['id'],s['id'],status='awaiting_review' if human else 'paused',
                                 candidate=s['result'] or s['candidate'],result=[],
                                 error_code='quality' if human else 'agent_stopped',error=decision.reason)
            if decision.action=='needs_user' and s:
                from .user_decisions import create
                create(store,w,s,decision.user_request or {'kind':'quality','question':decision.reason},decision.replacement)
            workflows.state(store,turn_id,'failed' if decision.action=='stop' else 'paused',error=decision.reason)
        if s:workflows.quality_event(store,w,s,'agent',decision.action,decision.model_dump_json())
    if decision.action=='pass' and s and not s['quality'].get('requires_review'):
        adapter.publish(w,s['candidate'])
    return True


def recover(store, w):
    """Review runs have no provider side effects; uncommitted decisions are discarded."""
    request=current(store,w['id'])
    if not request:return False
    from . import workflows
    store.execute("UPDATE workflow_agent_reviews SET status='pending',decision='{}',updated=? WHERE id=?",(store.now(),request['id']))
    if request['runs']>=3:
        store.execute("UPDATE workflow_agent_reviews SET status='exhausted' WHERE id=?",(request['id'],))
        workflows.state(store,w['turn_id'],'failed',error='Agent 验收多次中断，请重新规划。')
    else:
        workflows.state(store,w['turn_id'],'resuming' if request['purpose']=='final' else 'reviewing',text='正在恢复未完成的 Agent 验收；已完成步骤不会重跑。')
    return True
