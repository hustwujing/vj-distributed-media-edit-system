"""Durable Agent handoff at a quiescent boundary; workflow execution is retained.

The coordinator never kills a process from an API thread. Existing Agent work
finishes or is cancelled through the usual runtime; a pending switch fences
the next Agent claim until cleanup and unknown effects have been reconciled.
"""
from dataclasses import replace
import json
import uuid
from .agent_profiles import AgentProfile, for_task, selected

PENDING = ('requested', 'waiting', 'blocked')


def initialize(db):
    db.executescript('''
      CREATE TABLE IF NOT EXISTS agent_harness_switches(
        id TEXT PRIMARY KEY, project TEXT NOT NULL, task_id TEXT NOT NULL,
        request_key TEXT NOT NULL, source_config TEXT NOT NULL, target_config TEXT NOT NULL,
        source_digest TEXT NOT NULL, target_digest TEXT NOT NULL, state TEXT NOT NULL,
        reason TEXT NOT NULL DEFAULT '', handoff TEXT NOT NULL DEFAULT '{}',
        checkpoint TEXT NOT NULL DEFAULT '', event_cursor INTEGER NOT NULL DEFAULT 0,
        created REAL NOT NULL, updated REAL NOT NULL, UNIQUE(task_id,request_key));
      CREATE UNIQUE INDEX IF NOT EXISTS agent_one_pending_switch ON agent_harness_switches(task_id)
        WHERE state IN ('requested','waiting','blocked');
      CREATE TABLE IF NOT EXISTS agent_native_sessions(
        project TEXT PRIMARY KEY, harness TEXT NOT NULL, adapter_version INTEGER NOT NULL,
        runtime_profile TEXT NOT NULL, configuration_digest TEXT NOT NULL,
        native_session_id TEXT NOT NULL, restore_mode TEXT NOT NULL, updated REAL NOT NULL);
    ''')


def public(row):
    if not row:
        return None
    return {key: row[key] for key in ('id', 'task_id', 'state', 'reason', 'created', 'updated')} | {
        'source': AgentProfile(**json.loads(row['source_config'])).public(),
        'target': AgentProfile(**json.loads(row['target_config'])).public(),
    }


def latest(store, task):
    rows = store.query('SELECT * FROM agent_harness_switches WHERE task_id=? ORDER BY created DESC,id DESC LIMIT 1', (task,))
    return public(rows[0]) if rows else None


def request(store, project, task, target_name, request_key, expected_digest):
    if not isinstance(request_key, str) or not 1 <= len(request_key) <= 100:
        raise ValueError('切换请求编号无效。')
    target = selected(target_name)
    from .harness_adapters import get
    if not get(target.harness).available():
        raise ValueError('目标 Agent 运行时未安装或版本不兼容。')
    target.credentials()
    with store.connect() as db:
        db.execute('BEGIN IMMEDIATE')
        row = db.execute('SELECT * FROM agent_turns WHERE id=? AND project=?', (task, project)).fetchone()
        if not row:
            raise ValueError('任务不属于当前项目。')
        previous = db.execute('SELECT * FROM agent_harness_switches WHERE task_id=? AND request_key=?', (task, request_key)).fetchone()
        current = for_task(store, task)
        if not db.execute('SELECT task_id FROM agent_run_profiles WHERE task_id=?', (task,)).fetchone():
            db.execute('INSERT INTO agent_run_profiles(task_id,configuration,digest,created) VALUES (?,?,?,?)',
                       (task,json.dumps(current.snapshot()),current.digest,store.now()))
        # Switching implementation cannot reset or enlarge the current task budget.
        target = replace(target, budget=dict(current.budget))
        if previous:
            if previous['target_digest'] != target.digest:
                raise ValueError('同一请求编号不能改为另一切换目标。')
            return public(dict(previous))
        if current.digest != expected_digest:
            raise ValueError('Agent 配置已变化，请刷新后再切换。')
        if row['status'] in ('cancelling', 'cancelled'):
            raise ValueError('任务正在停止或已停止，不能接管。')
        if db.execute("SELECT id FROM agent_harness_switches WHERE task_id=? AND state IN ('requested','waiting','blocked')", (task,)).fetchone():
            raise ValueError('已有切换请求，请等待或撤销后再选择。')
        identity = uuid.uuid4().hex
        state = 'unchanged' if current.digest==target.digest else 'next_turn' if row['status'] in ('succeeded', 'failed') else 'requested'
        timestamp = store.now()
        db.execute('''INSERT INTO agent_harness_switches
          (id,project,task_id,request_key,source_config,target_config,source_digest,target_digest,state,created,updated)
          VALUES (?,?,?,?,?,?,?,?,?,?,?)''',
                   (identity, project, task, request_key, json.dumps(current.snapshot()), json.dumps(target.snapshot()),
                    current.digest, target.digest, state, timestamp, timestamp))
        db.execute('INSERT INTO agent_project_profiles VALUES (?,?) ON CONFLICT(project) DO UPDATE SET name=excluded.name',
                   (project, target.name))
    return latest(store, task)


def cancel(store, project, task, identity):
    with store.connect() as db:
        db.execute('BEGIN IMMEDIATE')
        row = db.execute('SELECT * FROM agent_harness_switches WHERE id=? AND project=? AND task_id=?', (identity, project, task)).fetchone()
        if not row:
            raise ValueError('切换请求不存在或不属于当前任务。')
        if row['state'] == 'cancelled':
            return public(dict(row))
        if row['state'] not in PENDING:
            raise ValueError('切换已经生效，不能撤销历史记录。')
        db.execute("UPDATE agent_harness_switches SET state='cancelled',reason='用户撤销切换',updated=? WHERE id=?", (store.now(), identity))
        source = AgentProfile(**json.loads(row['source_config']))
        target = AgentProfile(**json.loads(row['target_config']))
        db.execute('UPDATE agent_project_profiles SET name=? WHERE project=? AND name=?', (source.name, project, target.name))
    return latest(store, task)


def pending(db, task):
    if not db.execute("SELECT name FROM sqlite_master WHERE name='agent_harness_switches'").fetchone():return False
    return db.execute("SELECT id FROM agent_harness_switches WHERE task_id=? AND state IN ('requested','waiting','blocked')", (task,)).fetchone() is not None


def _unresolved_tools(store, task):
    rows = store.query("SELECT payload FROM agent_events WHERE run_id=? AND kind='tool' ORDER BY seq DESC LIMIT 1001", (task,))
    if len(rows) > 1000:
        return True
    latest_tools = {}
    for row in rows:
        value = json.loads(row['payload'])
        key = ((value.get('execution') or {}).get('attempt_id', ''), value.get('id'))
        latest_tools.setdefault(key, value.get('status'))
    return any(state == 'running' for state in latest_tools.values())


def reconcile(store, task=None):
    query = "SELECT id,task_id FROM agent_harness_switches WHERE state IN ('requested','waiting','blocked')"
    rows = store.query(query + (' AND task_id=?' if task else '') + ' ORDER BY created LIMIT 50', (task,) if task else ())
    for entry in rows:
        with store.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            switch = db.execute('SELECT * FROM agent_harness_switches WHERE id=?', (entry['id'],)).fetchone()
            if not switch or switch['state'] not in PENDING:
                continue
            turn = db.execute('SELECT * FROM agent_turns WHERE id=? AND project=?', (switch['task_id'], switch['project'])).fetchone()
            reason = ''
            if not turn or turn['status'] in ('cancelling', 'cancelled'):
                state, reason = 'cancelled', '原任务停止，切换不再激活执行。'
            else:
                active = db.execute('''SELECT a.id FROM execution_attempts a WHERE a.run_id=? AND a.kind='agent'
                  AND (EXISTS(SELECT 1 FROM execution_leases l WHERE l.attempt=a.id AND l.active=1 AND l.expires>?)
                  OR EXISTS(SELECT 1 FROM execution_cleanup c WHERE c.attempt=a.id AND c.state<>'complete')
                  OR (a.status='running' AND a.runtime<>'' AND NOT EXISTS(
                    SELECT 1 FROM execution_cleanup c WHERE c.attempt=a.id AND c.state='complete'))) LIMIT 1''', (turn['id'],store.now())).fetchone()
                if active:
                    state, reason = 'waiting', '等待当前 Agent 结束并完成资源回收；已提交的工作流继续推进。'
                elif turn['status'] in ('succeeded', 'failed'):
                    state, reason = 'next_turn', '本轮已经结束，后续任务使用所选 Agent。'
                elif turn['status']=='running':
                    state, reason = 'blocked', '旧 Agent 异常中断且尚未完成状态核实；请先恢复或核实原任务。'
                else:
                    unknown = db.execute('''SELECT request_key FROM workflow_provider_calls WHERE
                      (workflow=? OR workflow IN (SELECT id FROM agent_workflows WHERE run_id=?))
                      AND state IN ('submitting','unknown','submission_unknown') LIMIT 1''', (turn['id'], turn['id'])).fetchone()
                    if unknown or _unresolved_tools(store, turn['id']):
                        state, reason = 'blocked', '存在结果尚未核实的调用，保留任务；请先核实或撤销切换，不会重复执行。'
                    elif for_task(store, turn['id']).digest != switch['source_digest']:
                        state, reason = 'failed', '任务配置已发生其他变化，未激活过期的切换请求。'
                    else:
                        from .harness_context import build
                        from .harness_adapters import get
                        target = AgentProfile(**json.loads(switch['target_config']))
                        if not get(target.harness).available():
                            state, reason = 'blocked', '当前节点缺少目标 Agent 运行时，等待兼容执行器。'
                        else:
                            try:target.credentials()
                            except ValueError:
                                db.execute("UPDATE agent_harness_switches SET state='blocked',reason='目标模型凭据当前不可用，未切换执行配置。',updated=? WHERE id=?",(store.now(),switch['id']))
                                continue
                            handoff = build(store, turn['project'], turn['id'])
                            from .egress_policy import scrub,secrets
                            encoded_handoff=scrub(json.dumps(handoff,ensure_ascii=False),secrets())
                            checkpoint = db.execute('SELECT path FROM execution_checkpoints WHERE project=?', (turn['project'],)).fetchone()
                            cursor = db.execute('SELECT MAX(seq) n FROM agent_events WHERE run_id=?', (turn['id'],)).fetchone()['n'] or 0
                            db.execute('UPDATE agent_run_profiles SET configuration=?,digest=?,generation=generation+1 WHERE task_id=? AND digest=?',
                                       (switch['target_config'], switch['target_digest'], turn['id'], switch['source_digest']))
                            db.execute('DELETE FROM agent_sessions WHERE project=?', (turn['project'],))
                            db.execute('DELETE FROM execution_session_profiles WHERE project=?', (turn['project'],))
                            db.execute('DELETE FROM agent_native_sessions WHERE project=?', (turn['project'],))
                            db.execute('UPDATE agent_harness_switches SET handoff=?,checkpoint=?,event_cursor=? WHERE id=?',
                                       (encoded_handoff, checkpoint['path'] if checkpoint else '', cursor, switch['id']))
                            state, reason = 'activated', '已切换执行配置；新 Agent 将加载公开上下文和已有成果，不重放历史操作。'
                            # No workflow/step status, acceptance or authorization is changed here.
            db.execute('UPDATE agent_harness_switches SET state=?,reason=?,updated=? WHERE id=?',
                       (state, reason, store.now(), switch['id']))
            if state != switch['state']:
                from .agent_store import event
                event(store, switch['task_id'], 'harness_switch', {'switch_id': switch['id'], 'state': state, 'reason': reason})


def record_session(store, project, profile, identity, restore_mode):
    from .runtime_image import fingerprint
    store.execute('''INSERT INTO agent_native_sessions VALUES (?,?,?,?,?,?,?,?)
      ON CONFLICT(project) DO UPDATE SET harness=excluded.harness,adapter_version=excluded.adapter_version,
      runtime_profile=excluded.runtime_profile,configuration_digest=excluded.configuration_digest,
      native_session_id=excluded.native_session_id,restore_mode=excluded.restore_mode,updated=excluded.updated''',
                  (project, profile.harness, profile.adapter_version, fingerprint(), profile.digest, identity, restore_mode, store.now()))
    from .task_leases import CURRENT
    lease=CURRENT.get()
    if lease:
        store.execute('UPDATE agent_attempt_profiles SET native_session_id=?,restore_mode=? WHERE attempt_id=?',
                      (identity,restore_mode,lease.attempt))
