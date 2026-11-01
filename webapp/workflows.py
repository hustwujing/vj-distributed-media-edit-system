"""Durable, project-scoped steps. One AgentHost owns scheduling per database.

External submission is deliberately at-most-once: an interrupted POST with no
saved provider id is paused, never guessed or silently submitted a second time.
"""
from __future__ import annotations

import json
import re
import sqlite3
import time
import uuid
import os
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from . import agent_store
from .quality_policy import Acceptance,Repair,validate_typed_output_targets
from .operations import InputRef


class Step(BaseModel):
    model_config = ConfigDict(extra='forbid')
    id: str = Field(pattern=r'^[a-z][a-z0-9_-]{0,39}$')
    name: str = Field(min_length=1, max_length=120)
    kind: Literal['script','operation']
    operation: str | None = None
    implementation: str | None = None
    contract_version: int = Field(default=1,ge=1,le=1)
    inputs: dict[str,InputRef] = Field(default_factory=dict,max_length=20)
    parameters: dict = Field(default_factory=dict)
    output_roles: dict[str,str] = Field(default_factory=dict,max_length=20)
    preflight: list[str] = Field(default_factory=list,max_length=20)
    config_files: list[str] = Field(default_factory=list,max_length=20)
    depends_on: list[str] = Field(default_factory=list, max_length=32)
    argv: list[str] = Field(default_factory=list, max_length=100)
    outputs: list[str] = Field(default_factory=list, max_length=20)
    timeout: int = Field(default=900, ge=10, le=3600)
    retry_safe: bool = False
    acceptance:list[Acceptance]=Field(default_factory=list,max_length=20)
    repairs:list[Repair]=Field(default_factory=list,max_length=2)
    goal: str = Field(default='', max_length=2000)
    basic_reason: str = Field(default='', max_length=1000)
    on_failure: Literal['pause','diagnose'] = 'pause'

    @model_validator(mode='after')
    def valid_operation(self):
        if self.kind=='operation':
            from .operations import get,validate_parameters
            op=get(self.operation,self.implementation)
            from .operation_lifecycle import for_operation
            self.implementation=for_operation(op).id
            if self.argv or self.outputs:raise ValueError('operation 不能混合 argv/outputs')
            if self.retry_safe and not op.retry_safe:raise ValueError('此操作不允许自动重提')
            if self.repairs:raise ValueError('固定操作不能声明任意修复命令')
            self.parameters=validate_parameters(self.operation,self.parameters,self.inputs,self.implementation)
            if op.output_files is not None:
                invalid=[check.file for check in self.acceptance if check.file!='*' and check.file not in op.output_files]
                if invalid:raise ValueError('验收文件不属于该 operation 的输出契约：'+','.join(invalid)+'；实际支持：'+','.join(op.output_files))
                validate_typed_output_targets(self.acceptance,op.output_files)

        elif self.operation or self.parameters or self.implementation:raise ValueError('只有 operation 步骤接受 operation/parameters/implementation')
        if self.kind == 'script':
            if not self.argv or not self.outputs:
                raise ValueError('本地步骤需要 argv 和 outputs')
            if any(not arg or len(arg) > 8000 or '\x00' in arg for arg in self.argv):
                raise ValueError('无效的命令参数')
            if any(not re.fullmatch(r'[A-Za-z0-9_\u3400-\u9fff][A-Za-z0-9_.\u3400-\u9fff-]{0,99}', n)
                   or '.' not in n for n in self.outputs):
                raise ValueError('outputs 只能使用文件名，不得包含目录')
            if any(check.file!='*' and check.file not in self.outputs for check in self.acceptance):
                raise ValueError('验收条件必须引用声明的输出')
            validate_typed_output_targets(self.acceptance,self.outputs)
        if any(not re.fullmatch('[a-z][a-z0-9_-]{0,39}',name) for name in self.inputs):raise ValueError('输入角色名无效')
        for role,name in self.output_roles.items():
            if not re.fullmatch('[a-z][a-z0-9_-]{0,39}',role) or name not in self.outputs:
                raise ValueError('输出角色必须引用声明的本地输出文件')
        for path in self.preflight+self.config_files:
            if not path.startswith('edit/') or '..' in Path(path).parts or Path(path).is_absolute():raise ValueError('脚本与配置必须位于 edit 目录')
        return self


def validate_plan(raw):
    """Validate the canonical script/operation submission contract."""
    plan=Plan.model_validate(raw)
    errors=[]
    for step, original in zip(plan.steps,raw['steps']):
        prefix=step.id+': '
        if not step.goal.strip():errors.append(prefix+'请填写 goal，说明本步如何满足已确认需求')
        if 'on_failure' not in original:errors.append(prefix+'请明确 on_failure: pause 或 diagnose')
        if not step.acceptance:errors.append(prefix+'请声明 acceptance；只需基础检查时使用 file 并填写 basic_reason')
        meaningful=any(c.kind in ('agent','review','text','json','subtitle') or any(getattr(c,k) is not None for k in
            ('width','height','audio','video','min_duration','max_duration','max_bytes','has_alpha','min_transparent_fraction','fps')) for c in step.acceptance)
        if not meaningful and not step.basic_reason.strip():errors.append(prefix+'仅基础检查需要说明 basic_reason')
        if step.kind=='script':
            uncovered=[f for f in step.outputs if not any(c.file in ('*',f) for c in step.acceptance)]
            if uncovered:errors.append(prefix+'这些产物没有验收条件: '+', '.join(uncovered))
        if any(not repair.when for repair in step.repairs):errors.append(prefix+'备用修复必须声明 when 错误代码')
        if step.kind=='operation':
            from .operations import get
            selected=get(step.operation,step.implementation)
            if not selected.available():errors.append(prefix+'所需服务未配置: '+step.operation)
            elif selected.executor=='image_sync' and step.parameters.get('fit','native')=='native' and any(c.width is not None or c.height is not None for c in step.acceptance):
                errors.append(prefix+'上游原始尺寸不保证；精确尺寸要求需显式选择 fit: contain/cover，或另设本地调整步骤')
            elif selected.executor=='generation_async':
                from .generation_provider import validate
                try:validate(step.operation,step.parameters)
                except Exception as exc:errors.append(prefix+str(exc))
    if errors:raise ValueError('\n'.join(errors))
    return plan


class Plan(BaseModel):
    model_config = ConfigDict(extra='forbid')
    key: str = Field(pattern=r'^[a-zA-Z0-9_-]{1,64}$')
    name: str = Field(min_length=1, max_length=120)
    steps: list[Step] = Field(min_length=1, max_length=32)

    @model_validator(mode='after')
    def valid_graph(self):
        seen = {}
        from .operations import for_step
        for step in self.steps:
            if step.id in seen or not set(step.depends_on).issubset(seen):
                raise ValueError('步骤 ID 必须唯一，依赖必须指向前面的步骤')
            for name,ref in step.inputs.items():
                if ref.step:
                    if ref.step not in step.depends_on:raise ValueError('步骤输入必须声明 depends_on')
                    parent=seen[ref.step];operation=for_step(parent.model_dump())
                    available=set(operation.outputs) if operation else set(parent.output_roles)
                    if ref.role not in available:
                        raise ValueError(f'{step.id} 的输入 {name} 引用了 {ref.step} 未声明的输出角色 {ref.role}；请先声明 output_roles，每个角色对应一个输出文件')
            seen[step.id]=step
        operations=[for_step(s.model_dump()) for s in self.steps]
        if sum(bool(op and op.provider and op.id.startswith('image.')) for op in operations)>4:raise ValueError('每个执行轮最多安排4次外部图片调用')
        if sum(bool(op and op.provider and op.id.startswith('video.')) for op in operations)>int(os.environ.get('VIDEO_USE_TASK_VIDEO_CALLS','2')):raise ValueError('视频生成超过本任务调用额度')
        return self


def initialize(store):
    with store.connect() as db:
        db.executescript('''
        CREATE TABLE IF NOT EXISTS agent_workflows (
          id TEXT PRIMARY KEY, project TEXT NOT NULL, turn_id TEXT NOT NULL UNIQUE,
          plan_id TEXT NOT NULL, request_key TEXT NOT NULL, name TEXT NOT NULL,
          definition TEXT NOT NULL, status TEXT NOT NULL, error TEXT NOT NULL DEFAULT '',
          created REAL NOT NULL, updated REAL NOT NULL
        );
        CREATE TABLE IF NOT EXISTS agent_steps (
          workflow TEXT NOT NULL, id TEXT NOT NULL, position INTEGER NOT NULL,
          spec TEXT NOT NULL, status TEXT NOT NULL, external_id TEXT,
          attempts INTEGER NOT NULL DEFAULT 0, result TEXT NOT NULL DEFAULT '[]',
          error TEXT NOT NULL DEFAULT '', error_code TEXT NOT NULL DEFAULT '',
          next_poll REAL NOT NULL DEFAULT 0, started REAL, updated REAL NOT NULL,
          PRIMARY KEY(workflow,id)
        );
        CREATE TABLE IF NOT EXISTS workflow_control(turn_id TEXT PRIMARY KEY,pause_requested INTEGER NOT NULL DEFAULT 0);
        CREATE INDEX IF NOT EXISTS agent_workflow_status ON agent_workflows(status);
        CREATE TABLE IF NOT EXISTS workflow_step_versions(
          workflow TEXT NOT NULL,step TEXT NOT NULL,revision INTEGER NOT NULL,
          spec TEXT NOT NULL,files TEXT NOT NULL,quality TEXT NOT NULL,
          inputs TEXT NOT NULL,created REAL NOT NULL,PRIMARY KEY(workflow,step,revision)
        );
        CREATE TABLE IF NOT EXISTS workflow_quality_events(
          id TEXT PRIMARY KEY,workflow TEXT NOT NULL,step TEXT NOT NULL,revision INTEGER NOT NULL,
          actor TEXT NOT NULL,action TEXT NOT NULL,feedback TEXT NOT NULL,created REAL NOT NULL
        );
        CREATE TABLE IF NOT EXISTS workflow_diagnostics(
          id TEXT PRIMARY KEY,workflow TEXT NOT NULL,step TEXT NOT NULL,revision INTEGER NOT NULL,
          attempt INTEGER NOT NULL,record TEXT NOT NULL,created REAL NOT NULL
        );
        CREATE TABLE IF NOT EXISTS workflow_agent_reviews(
          id TEXT PRIMARY KEY,workflow TEXT NOT NULL,step TEXT,purpose TEXT NOT NULL,
          status TEXT NOT NULL,snapshot TEXT NOT NULL,decision TEXT NOT NULL DEFAULT '{}',
          runs INTEGER NOT NULL DEFAULT 0,created REAL NOT NULL,updated REAL NOT NULL
        );
        CREATE UNIQUE INDEX IF NOT EXISTS workflow_one_review ON workflow_agent_reviews(workflow)
          WHERE status IN ('pending','running','staged');
        ''')
        if 'failures' not in {r['name'] for r in db.execute('PRAGMA table_info(agent_steps)')}:
            try:db.execute('ALTER TABLE agent_steps ADD COLUMN failures INTEGER NOT NULL DEFAULT 0')
            except sqlite3.OperationalError:
                if 'failures' not in {r['name'] for r in db.execute('PRAGMA table_info(agent_steps)')}:raise
        from .provider_calls import initialize as initialize_calls
        initialize_calls(db)
        from .user_decisions import initialize as initialize_requests
        initialize_requests(db)
        from .manual_async import initialize as initialize_manual
        initialize_manual(db)
        from .step_checkpoints import initialize as initialize_step_checkpoints
        initialize_step_checkpoints(db)
        columns={r['name'] for r in db.execute('PRAGMA table_info(agent_steps)')}
        for name,definition in {'revision':'INTEGER NOT NULL DEFAULT 0','repair_count':'INTEGER NOT NULL DEFAULT 0',
                                'candidate':"TEXT NOT NULL DEFAULT '[]'",'quality':"TEXT NOT NULL DEFAULT '{}'",
                                'bindings':"TEXT NOT NULL DEFAULT '{}'",'wait_reason':"TEXT NOT NULL DEFAULT ''"}.items():
            if name not in columns:db.execute(f'ALTER TABLE agent_steps ADD COLUMN {name} {definition}')
        # New deployments use the canonical contract; no historical data rewrite.


def get(store, turn_id):
    try:rows = store.query('SELECT * FROM agent_workflows WHERE turn_id=?', (turn_id,))
    except sqlite3.OperationalError as exc:
        if 'no such table: agent_workflows' in str(exc):return None
        raise
    if not rows:
        return None
    w = rows[0]
    w['definition'] = json.loads(w['definition'])
    w['steps'] = store.query('SELECT * FROM agent_steps WHERE workflow=? ORDER BY position', (w['id'],))
    diagnostics={}
    records=store.query('''SELECT step,record FROM (
        SELECT step,record,ROW_NUMBER() OVER (PARTITION BY step ORDER BY created DESC,id DESC) AS item_rank
        FROM workflow_diagnostics WHERE workflow=?) ranked WHERE item_rank<=3 ORDER BY step,item_rank''',(w['id'],))
    for record in records:diagnostics.setdefault(record['step'],[]).append(json.loads(record['record']))
    for s in w['steps']:
        s['spec'] = json.loads(s['spec'])
        s['result'] = json.loads(s['result'])
        s['candidate']=json.loads(s['candidate'])
        s['quality']=json.loads(s['quality'])
        s['bindings']=json.loads(s['bindings'])
        s['diagnostics']=diagnostics.get(s['id'],[])
    from .workflow_review import current
    w['agent_review']=current(store,w['id'])
    from .user_decisions import pending
    w['user_requests']=pending(store,w['id'])
    if w['status']=='paused' and w['agent_review']:
        from .task_leases import quota_status,model_call_limit
        quota=quota_status(store,turn_id,'model',model_call_limit())
        if quota['blocked']:w['resume_block']=quota
    return w


def public(w):
    if not w:
        return None
    return {k: w[k] for k in ('id', 'name', 'status', 'error', 'plan_id')} | {
        'user_requests':w.get('user_requests',[]),
        'agent_review': {k:w['agent_review'][k] for k in ('id','step','purpose','status','runs')} if w['agent_review'] else None,
        'steps': [{k: s[k] for k in ('id', 'status', 'attempts', 'error', 'error_code','revision','repair_count','quality','diagnostics','wait_reason')} |
                  {'name': s['spec']['name'], 'kind': s['spec']['kind'], 'operation':s['spec'].get('operation','script'), 'updated':s['updated'],
                   'wait_reason':s.get('wait_reason') or ('dependency' if s['status']=='pending' and any(parent['id'] in s['spec']['depends_on'] and parent['status']!='succeeded' for parent in w['steps']) else 'user' if s['status']=='awaiting_review' else ''),'preflight':s.get('bindings',{}).get('_preflight'),'depends_on': s['spec']['depends_on'], 'files': s['result'],'candidate_files':s['candidate']} for s in w['steps']],
        'resume_block':w.get('resume_block'),
        'resumable': w['status'] == 'paused' and not w.get('resume_block') and (bool(w['agent_review'] and w['agent_review']['runs']<3) or (not w['agent_review'] and all(
            s['error_code'] in ('', 'poll', 'collect', 'local','validate','pre_submit') and
            (s['error_code'] != 'local' or s['spec']['retry_safe'] and s['attempts'] < 3)
            for s in w['steps'])))}


def submit(store, turn_id, raw):
    from .execution_intent import get as get_intent
    intent=get_intent(store,turn_id)
    if intent and isinstance(raw,dict) and isinstance(raw.get('steps'),list):
        import copy
        raw=copy.deepcopy(raw)
        for step in raw['steps']:
            if isinstance(step,dict) and step.get('kind')=='operation' and not step.get('implementation'):
                selected=intent.get('implementations',{}).get(step.get('operation'))
                if selected:step['implementation']=selected
    plan = validate_plan(raw)
    definition = plan.model_dump_json()
    with store.connect() as db:
        db.execute('BEGIN IMMEDIATE')
        row = db.execute('SELECT * FROM agent_turns WHERE id=?', (turn_id,)).fetchone()
        if not row or row['mode'] != 'execute' or row['status'] != 'running' or not row['plan_id']:
            raise ValueError('只有已确认方案的执行轮可以安排后台步骤')
        approved = db.execute("SELECT id FROM agent_turns WHERE id=? AND project=? AND mode='plan' AND status='succeeded'",
                              (row['plan_id'], row['project'])).fetchone()
        if not approved:
            raise ValueError('已确认方案不存在')
        from .execution_intent import get as get_intent
        intent=get_intent(store,turn_id)
        if intent:
            from collections import Counter
            from .operations import for_step,get as get_operation
            allowed=Counter(intent['operations'])
            requested=Counter((op.id if (op:=for_step(step.model_dump())) else 'script') for step in plan.steps)
            if any(name not in allowed for name in requested):raise ValueError('工作流包含已确认意图之外的操作，请重新规划')
            paid=set()
            for step in plan.steps:
                if step.kind!='operation':continue
                approved=intent.get('implementations',{}).get(step.operation)
                if approved and approved!=step.implementation:raise ValueError('工作流实现与已确认意图不一致，请重新规划')
                if for_step(step.model_dump()).provider:paid.add(step.operation)
            if any(requested[name]>allowed[name] for name in paid):
                raise ValueError('工作流超出已确认的外部调用次数')
        existing = db.execute('SELECT definition FROM agent_workflows WHERE turn_id=?', (turn_id,)).fetchone()
        if existing:
            if Plan.model_validate_json(existing['definition']).model_dump_json() != definition:
                raise ValueError('本轮已提交后台计划，不能替换或重复安排；请查询状态')
        else:
            assets = {a['id']: a for a in store.assets(row['project'])}
            for step in plan.steps:
                for ref in step.inputs.values():
                    if ref.asset and ref.asset not in assets:raise ValueError('输入素材不属于当前项目')
            now = store.now()
            wid = uuid.uuid4().hex
            db.execute('INSERT INTO agent_workflows VALUES (?,?,?,?,?,?,?,?,?,?,?)',
                       (wid, row['project'], turn_id, row['plan_id'], plan.key, plan.name,
                        definition, 'staged', '', now, now))
            for i, step in enumerate(plan.steps):
                db.execute('INSERT INTO agent_steps(workflow,id,position,spec,status,updated) VALUES (?,?,?,?,?,?)',
                           (wid, step.id, i, step.model_dump_json(), 'pending', now))
    return public(get(store, turn_id))


def change(store, wid, sid, **values):
    values['updated'] = store.now()
    if values.get('status') in ('verifying','waiting','succeeded','failed','paused','pending','cancelled'):
        from .task_usage import complete
        with store.connect() as db:
            calls=db.execute("SELECT call_id,task_id FROM usage_calls WHERE category='workflow' AND status='running' AND step_id=? AND task_id=(SELECT turn_id FROM agent_workflows WHERE id=?)",(sid,wid)).fetchall()
            for call in calls:
                outcome='cancelled' if values['status']=='cancelled' else 'failed' if values['status'] in ('failed','paused','pending') else 'succeeded'
                complete(db,call['task_id'],call['call_id'],outcome)
    keys = list(values)
    store.execute('UPDATE agent_steps SET '+','.join(k+'=?' for k in keys)+' WHERE workflow=? AND id=?',
                  tuple(json.dumps(values[k], ensure_ascii=False) if k in ('result','candidate','quality','spec','bindings') else values[k] for k in keys)+(wid, sid))
    store.execute('UPDATE agent_workflows SET updated=? WHERE id=?', (store.now(), wid))
    store.execute('UPDATE agent_turns SET updated=? WHERE id=(SELECT turn_id FROM agent_workflows WHERE id=?)', (store.now(), wid))


def state(store, turn_id, status, text=None, error=''):
    now = store.now()
    with store.connect() as db:
        db.execute('BEGIN IMMEDIATE')
        row = db.execute('SELECT status FROM agent_turns WHERE id=?', (turn_id,)).fetchone()
        # Cancellation wins over a concurrently finishing step or Agent response.
        if not row or row['status'] in ('cancelling', 'cancelled','failed'):
            return False
        db.execute('UPDATE agent_workflows SET status=?,error=?,updated=? WHERE turn_id=?',
                   (status, error, now, turn_id))
        turn_status = {'ready':'waiting', 'paused':'paused', 'resuming':'queued', 'reviewing':'queued', 'succeeded':'succeeded','failed':'failed'}[status]
        db.execute('UPDATE agent_turns SET status=?,error=?,updated=? WHERE id=?', (turn_status, error, now, turn_id))
        if text is not None:
            db.execute('UPDATE agent_turns SET text=? WHERE id=?', (text, turn_id))
        if status=='failed':
            db.execute("UPDATE workflow_agent_reviews SET status='cancelled' WHERE workflow IN (SELECT id FROM agent_workflows WHERE turn_id=?) AND status IN ('pending','running','staged')",(turn_id,))
            db.execute("UPDATE workflow_user_requests SET status='cancelled' WHERE workflow IN (SELECT id FROM agent_workflows WHERE turn_id=?) AND status='pending'",(turn_id,))
            from .task_lifecycle import request_cleanup
            request_cleanup(db,turn_id)
    agent_store.event(store, turn_id, 'progress', {'text': text or error or {
        'ready':'继续执行后续步骤。','succeeded':'最终验收通过，成果已交付。','paused':'任务已暂停，已有成果保留。',
        'reviewing':'正在验收或诊断当前步骤。','resuming':'后台步骤已完成，正在最终验收。','failed':'任务已终止，已保存成果保留。'}[status]})
    return True


def cancel(store, turn_id):
    from .task_lifecycle import cancel_task
    return cancel_task(store,turn_id)


def pause(store,turn_id):
    w=get(store,turn_id)
    if not w or w['status'] in ('succeeded','failed','cancelled','cancelling'):raise ValueError('当前工作流不能暂停')
    store.execute('INSERT INTO workflow_control VALUES (?,1) ON CONFLICT(turn_id) DO UPDATE SET pause_requested=1',(turn_id,))
    agent_store.event(store,turn_id,'progress',{'text':'已请求在当前安全边界暂停；不会取消已提交的上游任务。'})
    settle_pauses(store,turn_id)


def settle_pauses(store,turn_id=None):
    """A safe pause is control-plane work and must not wait for a compute slot."""
    candidates=store.query('''SELECT c.turn_id FROM workflow_control c
      JOIN agent_workflows w ON w.turn_id=c.turn_id JOIN agent_turns t ON t.id=c.turn_id
      WHERE c.pause_requested=1 AND w.status IN ('ready','reviewing','resuming')
      AND t.status IN ('queued','running','waiting')
      AND NOT EXISTS(SELECT 1 FROM execution_leases l WHERE l.turn_id=c.turn_id AND l.active=1 AND l.expires>?)
      AND NOT EXISTS(SELECT 1 FROM execution_cleanup x WHERE x.task_id=c.turn_id AND x.state<>'complete')'''+(' AND c.turn_id=?' if turn_id else '')+' LIMIT 100',
      (store.now(),turn_id) if turn_id else (store.now(),))
    changed=0
    for candidate in candidates:
        tid=candidate['turn_id']
        with store.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            eligible=db.execute('''SELECT c.turn_id FROM workflow_control c
              JOIN agent_workflows w ON w.turn_id=c.turn_id JOIN agent_turns t ON t.id=c.turn_id
              WHERE c.turn_id=? AND c.pause_requested=1 AND w.status IN ('ready','reviewing','resuming')
              AND t.status IN ('queued','running','waiting')''',(tid,)).fetchone()
            if not eligible:continue
            active=db.execute('SELECT attempt FROM execution_leases WHERE turn_id=? AND active=1 AND expires>? LIMIT 1',(tid,store.now())).fetchone()
            cleaning=db.execute("SELECT attempt FROM execution_cleanup WHERE task_id=? AND state<>'complete' LIMIT 1",(tid,)).fetchone()
            if active or cleaning:continue
            changed+=bool(state(store,tid,'paused',error='已在安全边界暂停，上游已提交任务可能继续运行。'))
    return changed


def resume(store, turn_id):
    with store.connect() as db:
        db.execute('BEGIN IMMEDIATE')
        w = get(store, turn_id)
        if w and w.get('resume_block'):
            from .task_leases import quota_error
            raise quota_error(w['resume_block'])
        if not w or not public(w)['resumable']:
            raise ValueError('此任务不能安全重试；外部提交结果不确定或生成失败时，请先停止并核实结果')
        row = db.execute('SELECT status FROM agent_turns WHERE id=?', (turn_id,)).fetchone()
        if row['status'] != 'paused':
            raise ValueError('任务已在继续或停止')
        from .task_lifecycle import check
        check(db,turn_id)
        db.execute('UPDATE workflow_control SET pause_requested=0 WHERE turn_id=?',(turn_id,))
        from . import workflow_review
        if w['agent_review']:
            workflow_review.recover(store,w)
            return
        db.execute("UPDATE agent_steps SET status=CASE WHEN error_code='validate' THEN 'verifying' WHEN external_id IS NULL THEN 'pending' ELSE 'waiting' END,error='',error_code='',next_poll=0,failures=0,started=? WHERE workflow=? AND status='paused'", (store.now(), w['id']))
        db.execute("UPDATE agent_workflows SET status='ready',error='',updated=? WHERE id=?", (store.now(), w['id']))
        db.execute("UPDATE agent_turns SET status='waiting',error='',updated=? WHERE id=?", (store.now(), turn_id))


def quality_event(store,w,s,actor,action,feedback):
    store.execute('INSERT INTO workflow_quality_events VALUES (?,?,?,?,?,?,?,?)',
        (uuid.uuid4().hex,w['id'],s['id'],s['revision'],actor,action,feedback,store.now()))


def queue_auto_repair(store,w,s,report):
    with store.connect() as db:
        db.execute('BEGIN IMMEDIATE')
        live=get(store,w['turn_id'])
        current=next(x for x in live['steps'] if x['id']==s['id'])
        if current['revision']!=s['revision']:return False
        options=current['spec'].get('repairs',[]);count=current['repair_count']
        if current['spec']['kind']!='script' or count>=len(options) or count>=int(os.environ.get('VIDEO_USE_MAX_REPAIRS','2')):return False
        codes={code for check in report.get('checks',[]) if check.get('passed') is False
               for code in check.get('codes',[check.get('code')]) if code}
        if report.get('review')=='rejected':codes.add('review')
        used={json.loads(r['feedback']).get('option') for r in store.query(
            "SELECT feedback FROM workflow_quality_events WHERE workflow=? AND step=? AND actor='policy' AND action='repair' AND feedback LIKE '{%'",(w['id'],s['id']))}
        choice=next((i for i,option in enumerate(options) if i not in used and set(option.get('when',[]))&codes),None)
        if choice is None:return False
        affected={s['id']}
        for child in live['steps']:
            if set(child['spec']['depends_on'])&affected:affected.add(child['id'])
        from .operations import for_step
        for child in live['steps']:
            if child['id'] not in affected:continue
            op=for_step(child['spec'])
            if op and not op.retry_safe and (child['attempts'] or child['external_id'] or child['result']):return False
        stale=set()
        for child in live['steps']:
            if child['id'] not in affected:continue
            stale.update(str(Path(f).relative_to('edit')) for f in child['result']+child['candidate'])
            spec={**child['spec'],'argv':options[choice]['argv']} if child['id']==s['id'] else child['spec']
            change(store,w['id'],child['id'],spec=spec,revision=child['revision']+1,repair_count=child['repair_count']+int(child['id']==s['id']),
                status='pending',attempts=0,result=[],candidate=[],quality={},bindings={},external_id=None,
                wait_reason='',error='',error_code='',next_poll=0,started=None)
        row=agent_store.turn(store,w['turn_id'])
        artifacts=[dict(a,quality_state='superseded',purpose='quality_candidate') if a['name'] in stale else a for a in row['artifacts']]
        db.execute('UPDATE agent_turns SET artifacts=? WHERE id=?',(json.dumps(artifacts),w['turn_id']))
        quality_event(store,w,s,'policy','repair',json.dumps({'option':choice,'reason':options[choice]['reason'],'affected':sorted(affected)}))
        agent_store.event(store,w['turn_id'],'progress',{'text':s['spec']['name']+'未通过验收，正在按匹配的本地方案修复：'+options[choice]['reason']})
    return True


def quality_action(store,turn_id,step_id,revision,action,actor,feedback='',replacement=None,confirm_external=False,_agent_commit=False):
    with store.connect() as db:
        db.execute('BEGIN IMMEDIATE')
        from .task_lifecycle import check
        check(db,turn_id)
        w=get(store,turn_id)
        if not w or w['status']!='paused':raise ValueError('任务尚未暂停，不能修改验收状态')
        active=db.execute('SELECT active,expires FROM execution_leases WHERE turn_id=? AND active=1 AND expires>? LIMIT 1',(turn_id,store.now())).fetchone()
        if not _agent_commit and active and active['active'] and active['expires']>store.now():raise ValueError('正在完成本次资源回收，请稍后操作')
        s=next((x for x in w['steps'] if x['id']==step_id),None)
        if not s or s['revision']!=revision:raise ValueError('步骤版本已更新，请刷新后查看')
        if action=='approve':
            requests=db.execute("SELECT kind FROM workflow_user_requests WHERE workflow=? AND step=? AND status='pending' AND kind<>'quality'",(w['id'],step_id)).fetchall()
            if requests:raise ValueError('此步骤等待特定用户决策，不能通过质量确认代替')
            if s['status']!='awaiting_review' or not s['quality'].get('technical_valid') or not s['quality'].get('passed'):
                raise ValueError('技术检查未通过，不能直接确认交付')
            versions={x['id']:x['revision'] for x in w['steps'] if x['id'] in s['spec']['depends_on']}
            if versions!=s['quality'].get('input_versions',{}):raise ValueError('输入版本已变化，需要重新验收')
            report={**s['quality'],'review':'approved','requires_review':False,'requires_agent':False,
                    'checks':[dict(c,passed=True,reviewer='user') if c.get('kind') in ('review','agent') else c for c in s['quality'].get('checks',[])]}
            change(store,w['id'],step_id,status='succeeded',result=s['candidate'],quality=report,error='',error_code='')
            names={str(Path(f).relative_to('edit')) for f in s['candidate']}
            row=agent_store.turn(store,turn_id)
            for artifact in row['artifacts']:
                if artifact['name'] in names:
                    artifact.pop('purpose',None);artifact['quality_state']='approved'
            db.execute('UPDATE agent_turns SET artifacts=? WHERE id=?',(json.dumps(row['artifacts']),turn_id))
            quality_event(store,w,s,actor,'approve',feedback)
            state(store,turn_id,'ready',text='此步骤质量已确认，继续后续处理。')
        elif action=='reject':
            if s['status']!='awaiting_review':raise ValueError('该步骤当前不等待质量确认')
            quality_event(store,w,s,actor,'reject',feedback)
            change(store,w['id'],step_id,quality={**s['quality'],'review':'rejected','feedback':feedback},error=feedback or '用户未认可当前效果')
            report={**s['quality'],'review':'rejected'}
            if queue_auto_repair(store,w,s,report):state(store,turn_id,'ready',text='正在执行已声明的本地修复方案。')
            elif s['spec'].get('on_failure')=='diagnose':
                from .workflow_review import queue
                queue(store,get(store,turn_id),s,'diagnose')
        elif action=='repair':
            if replacement is None:raise ValueError('请提供明确的修复步骤')
            spec=Step.model_validate(replacement).model_dump()
            original=Step.model_validate(s['spec']).model_dump()
            if any(spec[k]!=original[k] for k in ('id','kind','depends_on','outputs','acceptance','goal','basic_reason','on_failure','retry_safe','timeout','repairs','operation','implementation','inputs','output_roles','preflight','config_files')):
                raise ValueError('修复不能改变验收标准、需求、权限、依赖或输出契约')
            affected={step_id}
            for child in w['steps']:
                if set(child['spec']['depends_on'])&affected:affected.add(child['id'])
            from .operations import for_step
            paid=any(x['id'] in affected and for_step(x['spec']) and not for_step(x['spec']).retry_safe and (x['attempts'] or x['external_id'] or x['result']) for x in w['steps'])
            if paid and not confirm_external:raise ValueError('修复包含重新生成图片，需要明确确认外部调用')
            if s['repair_count']>=int(os.environ.get('VIDEO_USE_MAX_REPAIRS','2')):
                raise ValueError('目标步骤已达到修复次数上限，请重新规划')
            stale=set()
            for child in w['steps']:
                if child['id'] not in affected:continue
                stale.update(str(Path(f).relative_to('edit')) for f in child['result']+child['candidate'])
                # Input invalidation creates a revision, not a code repair of
                # every descendant. Each step retains its own repair allowance.
                change(store,w['id'],child['id'],status='pending',spec=spec if child['id']==step_id else child['spec'],
                       revision=child['revision']+1,repair_count=child['repair_count']+int(child['id']==step_id),attempts=0,external_id=None,
                       result=[],candidate=[],quality={},bindings={},wait_reason='',error='',error_code='',next_poll=0,started=None)
            turn=agent_store.turn(store,turn_id)
            artifacts=[dict(a,quality_state='superseded',purpose='quality_candidate') if a['name'] in stale else a for a in turn['artifacts']]
            db.execute('UPDATE agent_turns SET artifacts=? WHERE id=?',(json.dumps(artifacts),turn_id))
            quality_event(store,w,s,actor,'repair',json.dumps({'feedback':feedback,'replacement':spec,'affected':sorted(affected)},ensure_ascii=False))
            db.execute("UPDATE workflow_agent_reviews SET status='superseded',updated=? WHERE workflow=? AND status IN ('pending','running','staged')",(store.now(),w['id']))
            db.execute("UPDATE workflow_user_requests SET status='superseded' WHERE workflow=? AND status='pending'",(w['id'],))
            state(store,turn_id,'ready',text='修复版本已确认，将重新计算受影响的步骤。')
        else:raise ValueError('未知验收操作')
    return public(get(store,turn_id))


class Runner:
    """Runs one durable action per tick; waiting never occupies an Agent process."""
    def __init__(self, store, adapter, stop):
        self.store, self.adapter, self.stop = store, adapter, stop

    def recover(self, turn_id=None,step_id=None):
        # Production calls this only after atomically acquiring an expired lease.
        # The unscoped form remains available for offline maintenance/tests.
        query = "SELECT turn_id FROM agent_workflows WHERE status NOT IN ('succeeded','cancelled')"
        for row in self.store.query(query + (' AND turn_id=?' if turn_id else ''), (turn_id,) if turn_id else ()):
            w = get(self.store, row['turn_id'])
            turn = agent_store.turn(self.store, w['turn_id'])
            if w['status'] == 'cancelling' or turn['status'] in ('cancelling', 'cancelled'):
                cancel(self.store, w['turn_id'])
                continue
            from . import workflow_review
            if w['status'] in ('reviewing','resuming') and w['agent_review']:
                workflow_review.recover(self.store,w)
                continue
            if w['status'] == 'resuming' and turn['status'] == 'running':
                # An interrupted unrestricted Agent might have made side effects.
                state(self.store, w['turn_id'], 'paused', error='结果已保留；最终检查曾中断，可继续检查，勿重复生成。')
                continue
            if w['status'] == 'paused':
                continue
            if w['status']=='staged':
                from .task_leases import CURRENT
                lease=CURRENT.get()
                if lease and any(s['spec']['kind']=='script' for s in w['steps']):
                    previous=self.store.query('SELECT checkpoint FROM execution_attempts WHERE turn_id=? AND generation<? ORDER BY generation DESC LIMIT 1',
                                              (w['turn_id'],lease.generation))
                    if not previous or not previous[0]['checkpoint']:
                        for step in w['steps']:
                            change(self.store,w['id'],step['id'],status='paused',error_code='checkpoint_missing',
                                   error='步骤计划已保存，但执行脚本尚未形成完整检查点；请停止并重新规划。')
                        state(self.store,w['turn_id'],'paused',error='执行器在保存脚本前中断，未自动提交后续任务。')
                        continue
            uncertain = False
            for s in w['steps']:
                if step_id is not None and s['id']!=step_id:continue
                if s['status'] == 'paused':
                    uncertain = True
                elif s['status'] == 'submitting':
                    from .operations import for_step
                    op=for_step(s['spec'])
                    if op and op.executor in ('transcribe','image_sync'):
                        calls=self.store.query('SELECT state,result FROM workflow_provider_calls WHERE request_key=?',(w['id']+':'+s['id']+':'+str(s['revision']),))
                        if not calls:
                            change(self.store,w['id'],s['id'],status='pending');continue
                        if calls[0]['state']=='succeeded':
                            if json.loads(calls[0]['result']):
                                change(self.store,w['id'],s['id'],status='verifying',candidate=json.loads(calls[0]['result']));continue
                            change(self.store,w['id'],s['id'],status='paused',error_code='result_unavailable',error='上游已返回，但结果尚未持久化；未自动再次调用。')
                            uncertain=True;continue
                    change(self.store, w['id'], s['id'], status='paused', error_code='submission_unknown',
                           error='外部提交结果不确定，未自动重提；请核实服务端任务。')
                    uncertain = True
                elif s['status'] == 'running':
                    safe = s['spec']['retry_safe'] and s['attempts'] < 3
                    change(self.store, w['id'], s['id'], status='pending' if safe else 'paused',
                           error_code='' if safe else 'local', error='' if safe else '本地步骤中断，未声明可安全重试。')
                    uncertain |= not safe
                elif s['status'] == 'collecting':
                    change(self.store, w['id'], s['id'], status='waiting', next_poll=0)
            if uncertain:
                state(self.store, w['turn_id'], 'paused', error='任务中断，已保留完成步骤；需要核实未完成操作。')
            elif w['status'] != 'resuming':
                state(self.store, w['turn_id'], 'ready', text='正在从已保存的步骤继续处理。')

    def interrupted(self, turn_id):
        return self.stop.is_set() or agent_store.turn(self.store, turn_id)['status'] in ('cancelling', 'cancelled')

    def claim(self, w, s, status):
        with self.store.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            active = db.execute("SELECT w.id FROM agent_workflows w JOIN agent_turns t ON w.turn_id=t.id WHERE w.id=? AND w.status='ready' AND t.status='waiting'", (w['id'],)).fetchone()
            if not active or self.stop.is_set():
                return False
            claimed=bool(db.execute("UPDATE agent_steps SET status=?,wait_reason='',attempts=attempts+1,started=?,updated=? WHERE workflow=? AND id=? AND status='pending'",
                                   (status, self.store.now(), self.store.now(), w['id'], s['id'])).rowcount)
            if claimed:
                from .task_usage import start
                from .task_leases import CURRENT
                lease=CURRENT.get()
                start(db,w['turn_id'],'workflow',s['spec'].get('operation','script'),attempt=lease.attempt if lease else '',step=s['id'],revision=s['revision'])
            return claimed

    def tick(self, turn_id,step_id=None):
        w = get(self.store, turn_id)
        if not w or self.stop.is_set():
            return
        if agent_store.turn(self.store, turn_id)['status'] in ('cancelling', 'cancelled') or w['status'] == 'cancelling':
            self.cancel_steps(w)
            return
        if w['status'] != 'ready':
            return
        control=self.store.query('SELECT pause_requested FROM workflow_control WHERE turn_id=?',(turn_id,))
        if control and control[0]['pause_requested']:
            settle_pauses(self.store,turn_id)
            return
        self.store.execute('UPDATE agent_workflows SET updated=? WHERE id=?', (self.store.now(), w['id']))
        completed = {s['id'] for s in w['steps'] if s['status'] == 'succeeded'}
        if len(completed) == len(w['steps']):
            self.adapter.publish(w, [f for s in w['steps'] for f in s['result']])
            from .workflow_review import queue
            queue(self.store,w)
            return
        for s in w['steps']:
            if step_id is not None and s['id']!=step_id:continue
            if s['status'] not in ('pending', 'waiting','verifying') or not set(s['spec']['depends_on']).issubset(completed):
                continue
            if s['next_poll'] > self.store.now():
                continue
            self.step(w, s)
            return

    def step(self, w, s):
        sid, wid, tid = s['id'], w['id'], w['turn_id']
        spec = s['spec']
        from .operations import for_step
        operation=for_step(spec)
        from .operation_lifecycle import for_operation,Lifecycle
        implementation=for_operation(operation) if operation else None
        sync_provider=implementation is not None and implementation.lifecycle==Lifecycle.SYNC
        phase = 'validate' if s['status']=='verifying' else 'poll' if s['external_id'] else 'sync' if sync_provider else 'local' if implementation is None or implementation.lifecycle==Lifecycle.LOCAL else 'submit'
        if self.interrupted(tid):
            return
        try:
            if phase=='validate':
                files=s['candidate']
            elif phase == 'local':
                if not self.claim(w, s, 'running'):return
                agent_store.event(self.store, tid, 'progress', {'text':'正在执行：'+spec['name']})
                files = (implementation.invoke(self.adapter,'local',w,s,cancelled=lambda:self.interrupted(tid))
                         if implementation else self.adapter.local(w,s,lambda:self.interrupted(tid)))
            elif phase=='sync':
                if not self.claim(w,s,'submitting'):return
                files=implementation.invoke(self.adapter,'sync',w,s,cancelled=lambda:self.interrupted(tid))
            elif phase == 'submit':
                # Commit intent BEFORE the network call. A crash here is ambiguous,
                # even if no bytes reached the provider; recovery must not retry.
                if not self.claim(w, s, 'submitting'):return
                external_id = implementation.invoke(self.adapter,'submit',w,s)
                if not re.fullmatch(r'[A-Za-z0-9_-]{1,100}', external_id):
                    raise ValueError('外部任务 ID 无效')
                change(self.store, wid, sid, status='waiting',wait_reason='provider', external_id=external_id, next_poll=self.store.now()+2)
                agent_store.event(self.store, tid, 'progress', {'text':spec['name']+'已提交，后台等待结果；完成后将自动继续。'})
                return
            else:
                if s['started'] and self.store.now()-s['started'] > 86400:
                    raise RuntimeError('外部任务等待超过 24 小时，请核实后继续查询')
                record = implementation.invoke(self.adapter,'poll',w,s)
                change(self.store, wid, sid, failures=0)
                if record['status'] in ('failed', 'cancelled'):
                    phase = 'provider_failed'
                    from .generation_errors import task_failure
                    raise task_failure(record)
                if record['status'] != 'succeeded':
                    change(self.store, wid, sid, next_poll=self.store.now()+5)
                    return
                if self.interrupted(tid):
                    return
                phase = 'collect'
                change(self.store, wid, sid, status='collecting')
                files = implementation.invoke(self.adapter,'collect',w,s,record=record,cancelled=lambda:self.interrupted(tid))
            change(self.store,wid,sid,status='verifying',wait_reason='',candidate=files)
            phase='validate'
            report=self.adapter.validate(w,s,files) if hasattr(self.adapter,'validate') else {'version':'legacy-test','technical_valid':True,'passed':True,'requires_review':False,'files':[],'checks':[]}
            inputs={step['id']:step['revision'] for step in w['steps'] if step['id'] in spec['depends_on']}
            report['input_versions']=inputs
            from .workflow_inputs import roles
            try:report['output_roles']=roles(spec,files,getattr(files,'roles',None) or s.get('bindings',{}).get('_selected_roles'))
            except ValueError as role_error:
                if '多个候选' not in str(role_error):raise
                from .user_decisions import create
                role=str(role_error).rsplit(': ',1)[-1]
                choices=[f for f in files if Path(f).suffix.lower() in ('.png','.jpg','.jpeg','.webp')] if role=='primary_image' else [f for f in files if Path(f).suffix.lower() in ('.mp4','.mov','.webm','.mkv')]
                if not 1<len(choices)<=10:raise ValueError('候选过多或无法分类，请重新规划')
                change(self.store,wid,sid,quality=report,candidate=files,status='awaiting_review',error_code='quality',error='请选择本步用于后续处理的产物')
                latest=next(x for x in get(self.store,tid)['steps'] if x['id']==sid)
                create(self.store,w,latest,{'kind':'selection','question':'本步骤返回多个候选，请选择用于后续处理的结果。','options':[Path(f).name for f in choices],
                    'output_role':role,'candidates':{Path(f).name:f for f in choices}})
                if report['technical_valid'] and hasattr(self.adapter,'publish_candidate'):self.adapter.publish_candidate(w,files)
                state(self.store,tid,'paused',error='需要选择候选产物，未自动取第一项。')
                return
            with self.store.connect() as db:
                db.execute('BEGIN IMMEDIATE')
                current=db.execute('SELECT revision FROM agent_steps WHERE workflow=? AND id=?',(wid,sid)).fetchone()
                if current['revision']!=s['revision']:raise RuntimeError('验收版本已过期')
                change(self.store,wid,sid,quality=report)
                db.execute('INSERT OR IGNORE INTO workflow_step_versions VALUES (?,?,?,?,?,?,?,?)',
                    (wid,sid,s['revision'],json.dumps(spec),json.dumps(files),json.dumps(report),json.dumps(inputs),self.store.now()))
            if not report['passed'] or report['requires_review'] or report.get('requires_agent'):
                if report['technical_valid'] and hasattr(self.adapter,'publish_candidate'):self.adapter.publish_candidate(w,files)
                if not report['passed'] and queue_auto_repair(self.store,w,s,report):return
                from .workflow_review import queue
                if not report['passed'] and spec.get('on_failure')=='diagnose':
                    queue(self.store,get(self.store,tid),s,'diagnose')
                    return
                if report['passed'] and report.get('requires_agent'):
                    queue(self.store,get(self.store,tid),s,'review')
                    return
                change(self.store,wid,sid,status='awaiting_review',error_code='quality',
                       error='请检查候选结果并确认质量。' if report['passed'] else '产物未通过验收，请查看检查结果并调整。')
                state(self.store,tid,'paused',error='步骤质量需要确认，后续步骤尚未执行。')
                return
            # A successfully completed step remains a checkpoint even if stop was
            # clicked during its final write. Cancellation only prevents next steps.
            change(self.store, wid, sid, status='succeeded',wait_reason='', result=files, error='', error_code='')
            phase='publish'
            self.adapter.publish(w, files)
            agent_store.event(self.store, tid, 'progress', {'text':'已完成：'+spec['name']})
        except Exception as exc:
            from .task_lifecycle import TaskLimitReached
            from .task_leases import LeaseLost
            if isinstance(exc,(TaskLimitReached,LeaseLost)):raise
            if self.stop.is_set():
                return  # Leave the durable intent for startup reconciliation.
            if self.interrupted(tid):
                return  # Cancellation cleanup runs on the next tick.
            from .task_leases import TaskQuotaReached
            if isinstance(exc,TaskQuotaReached):phase='quota'
            if phase in ('submit','sync') and getattr(exc,'diagnostic',{}).get('code')=='provider_rejected':phase='provider_failed'
            if getattr(exc,'diagnostic',{}).get('code')=='pre_submit':phase='pre_submit'
            if phase=='sync':
                calls=self.store.query('SELECT state FROM workflow_provider_calls WHERE request_key=?',(wid+':'+sid+':'+str(s['revision']),))
                if not calls:phase='local'
                elif calls[0]['state']=='succeeded':phase='provider_failed'
            if phase in ('submit','sync'):
                from .provider_calls import update
                update(self.store,w,s,'submission_unknown')
            from .egress_policy import scrub,secrets
            detail=getattr(exc,'diagnostic',{'message':str(exc)[-4000:]})
            attempt_rows=self.store.query('SELECT attempts FROM agent_steps WHERE workflow=? AND id=?',(wid,sid))
            record={'phase':phase,'error_type':type(exc).__name__,'revision':s['revision'],'attempt':attempt_rows[0]['attempts'] if attempt_rows else s['attempts'],
                    'input_versions':{x['id']:x['revision'] for x in w['steps'] if x['id'] in spec['depends_on']},
                    **detail}
            encoded=scrub(json.dumps(record,ensure_ascii=False),secrets()).replace(str(self.store.root),'[project-data]')
            encoded=encoded.replace(str(Path(__file__).resolve().parents[1]),'[app]').replace('/data/wujing03/','[runtime]/').replace('/data1/wujing03/','[runtime]/')
            self.store.execute('INSERT INTO workflow_diagnostics VALUES (?,?,?,?,?,?,?)',
                (uuid.uuid4().hex,wid,sid,s['revision'],record['attempt'],encoded,self.store.now()))
            if phase=='publish':
                state(self.store,tid,'paused',error='步骤结果已保存，但发布未完成；请检查存储额度后继续，不会重新生成。')
                return
            messages = {'pre_submit':'提交前检查失败，本次未发起生成请求；可检查后继续。','quota':'工具调用额度已用完，本次未提交外部请求。','validate':'产物验收暂未完成，已暂停后续操作，可继续检查已有结果。',
                        'sync':'同步服务请求中断，未自动重提，请核实已有结果。',
                        'submit':'外部提交结果不确定，未自动重提；请核实后停止或重新规划。',
                        'poll':'查询外部任务失败，任务 ID 已保存；可继续查询，不会重新生成。',
                        'collect':'结果接收失败；可重新读取已有生成结果，不会重新生成。',
                        'local':'本地步骤未完成，已暂停后续操作；已完成结果保留。',
                        'provider_failed':'上游生成失败或取消，已暂停后续操作；不会自动重新生成。'}
            # Do not expose provider headers, credentials or host paths in errors.
            message = messages[phase]
            if phase == 'poll' and s['failures'] < 4 and (not s['started'] or self.store.now()-s['started'] <= 86400):
                change(self.store, wid, sid, status='waiting', failures=s['failures']+1,
                       next_poll=self.store.now()+min(60, 5*2**s['failures']), error=message)
                return
            from .execution_contracts import classify_error
            classification=classify_error(record,phase)
            if phase == 'local' and spec['retry_safe'] and classification not in ('code','environment','parameters') and s['attempts']+1 < 3:
                change(self.store, wid, sid, status='pending',wait_reason='retry', next_poll=self.store.now()+2, error=message)
                agent_store.event(self.store, tid, 'progress', {'text':spec['name']+'未完成，将在新输出目录重试。'})
                return
            change(self.store, wid, sid, status='paused', error=message,
                   error_code='submission_unknown' if phase in ('submit','sync') else phase)
            if phase in ('local','validate','provider_failed','pre_submit') and spec.get('on_failure')=='diagnose':
                from .workflow_review import queue
                queue(self.store,get(self.store,tid),s,'diagnose')
                return
            state(self.store, tid, 'paused', error=message)

    def cancel_steps(self, w):
        try:self.adapter.publish(w, [f for s in w['steps'] if s['status']=='succeeded' for f in s['result']])
        except Exception as exc:
            from .task_leases import LeaseLost
            from .task_lifecycle import TaskLimitReached
            if isinstance(exc,(LeaseLost,TaskLimitReached)):raise
            agent_store.event(self.store,w['turn_id'],'progress',{'text':'已完成检查点保留；部分结果暂未发布，不影响停止后续操作。'})
        for s in w['steps']:
            if s['status'] in ('succeeded', 'cancelled'):
                continue
            message = ''
            if s['external_id']:
                from .task_lifecycle import request_cleanup
                with self.store.connect() as db:request_cleanup(db,w['turn_id'])
                message = '已停止下游步骤，正在独立核实上游取消结果。'
            elif s['status'] == 'submitting' or s['error_code'] == 'submission_unknown':
                message = '外部提交结果不确定，上游可能仍在运行；未重新提交。'
            change(self.store, w['id'], s['id'], status='cancelled', error=message)
        self.store.execute("UPDATE agent_workflows SET status='cancelled',updated=? WHERE id=?", (self.store.now(), w['id']))
        row = agent_store.turn(self.store, w['turn_id'])
        agent_store.finish(self.store, w['turn_id'], 'cancelled', '已停止后续步骤，已完成的结果仍可预览和下载。', row['artifacts'])
        agent_store.event(self.store, w['turn_id'], 'progress', {'text':'后台任务已停止。'})
