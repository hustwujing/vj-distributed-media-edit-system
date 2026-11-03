"""Conversation data is always scoped through the owning project."""
import json
import time
import uuid
import sqlite3
from .artifacts import order_artifacts


def initialize(store):
    with store.connect() as db:
        from .agent_profiles import initialize as initialize_profiles
        from .harness_events import initialize as initialize_context
        initialize_profiles(db)
        initialize_context(db)
        from .harness_switching import initialize as initialize_switching
        initialize_switching(db)
        db.executescript('''
          CREATE TABLE IF NOT EXISTS agent_sessions (
            project TEXT PRIMARY KEY, thread_id TEXT NOT NULL
          );
          CREATE TABLE IF NOT EXISTS agent_turns (
            id TEXT PRIMARY KEY, project TEXT NOT NULL, mode TEXT NOT NULL,
            prompt TEXT NOT NULL, status TEXT NOT NULL, text TEXT NOT NULL DEFAULT '',
            error TEXT NOT NULL DEFAULT '', artifacts TEXT NOT NULL DEFAULT '[]',
            created REAL NOT NULL, updated REAL NOT NULL
          );
          CREATE INDEX IF NOT EXISTS agent_turns_project ON agent_turns(project, created);
          CREATE UNIQUE INDEX IF NOT EXISTS agent_one_active ON agent_turns(project)
            WHERE status IN ('queued','running','cancelling');
          CREATE TABLE IF NOT EXISTS agent_events (
            seq INTEGER PRIMARY KEY AUTOINCREMENT, run_id TEXT NOT NULL,
            kind TEXT NOT NULL, payload TEXT NOT NULL, created REAL NOT NULL
          );
          CREATE INDEX IF NOT EXISTS agent_events_turn ON agent_events(run_id, seq);
        ''')
        from .public_messages import initialize as initialize_messages
        initialize_messages(db)
        from .live_activity import initialize as initialize_activity
        initialize_activity(db)
        from .execution_intent import initialize as initialize_intents
        initialize_intents(db)
        from .plan_readiness import initialize as initialize_readiness
        initialize_readiness(db)
        columns={row['name'] for row in db.execute('PRAGMA table_info(agent_turns)')}
        if 'stream_cursor' not in columns:
            try:db.execute("ALTER TABLE agent_turns ADD COLUMN stream_cursor TEXT NOT NULL DEFAULT ''")
            except sqlite3.OperationalError:
                if 'stream_cursor' not in {r['name'] for r in db.execute('PRAGMA table_info(agent_turns)')}:raise
        for column,declaration in [('text_generation','INTEGER NOT NULL DEFAULT 0'),('text_sequence','INTEGER NOT NULL DEFAULT 0'),('text_phase',"TEXT NOT NULL DEFAULT 'idle'")]:
            if column not in columns:db.execute(f'ALTER TABLE agent_turns ADD COLUMN {column} {declaration}')
        for column in ('canvas_context','canvas_result'):
            if column not in columns:
                try:db.execute(f"ALTER TABLE agent_turns ADD COLUMN {column} TEXT NOT NULL DEFAULT 'null'")
                except sqlite3.OperationalError:
                    if column not in {r['name'] for r in db.execute('PRAGMA table_info(agent_turns)')}:raise
        if 'attachments' not in columns:
            try:
                db.execute("ALTER TABLE agent_turns ADD COLUMN attachments TEXT NOT NULL DEFAULT '[]'")
            except sqlite3.OperationalError:
                if 'attachments' not in {row['name'] for row in db.execute('PRAGMA table_info(agent_turns)')}:
                    raise
        if 'plan_id' not in columns:
            try:db.execute('ALTER TABLE agent_turns ADD COLUMN plan_id TEXT')
            except sqlite3.OperationalError:
                if 'plan_id' not in {row['name'] for row in db.execute('PRAGMA table_info(agent_turns)')}:raise
        db.execute("CREATE UNIQUE INDEX IF NOT EXISTS agent_one_active_v2 ON agent_turns(project) WHERE status IN ('queued','running','waiting','paused','cancelling')")
    from . import workflows
    workflows.initialize(store)
    from . import task_leases
    task_leases.initialize(store)


def add_turn(store, project, mode, prompt, attachments=None,canvas_context=None,plan_id=None,parent_task_id=None):
    now = store.now()
    run_id = uuid.uuid4().hex
    with store.connect() as db:
        db.execute('INSERT INTO agent_turns (id,project,mode,prompt,status,created,updated,attachments,canvas_context,plan_id) VALUES (?,?,?,?,?,?,?,?,?,?)',
                  (run_id, project, mode, prompt, 'queued', now, now,json.dumps(attachments or [],ensure_ascii=False),json.dumps(canvas_context,ensure_ascii=False),plan_id))
        from .observation_schema import register
        register(db,run_id,project,now)
        from .task_usage import attach
        attach(db,run_id,project,now,plan_id or parent_task_id)
        from .agent_profiles import freeze
        freeze(db,run_id,project,now,plan_id)
    from .task_lifecycle import ensure
    with store.connect() as db:ensure(db,run_id,now)
    return run_id


def turn(store, run_id):
    rows = store.query('SELECT * FROM agent_turns WHERE id=?', (run_id,))
    if not rows:
        return None
    row = rows[0]
    row['artifacts'] = json.loads(row['artifacts'])
    row['attachments'] = json.loads(row.get('attachments','[]'))
    for key in ('canvas_context','canvas_result'):row[key]=json.loads(row.get(key,'null'))
    from . import workflows
    row['workflow'] = workflows.public(workflows.get(store, run_id))
    from .task_lifecycle import snapshot
    row['execution']=snapshot(store,run_id)
    from .scheduling import snapshot as scheduling_snapshot
    if row['execution']:row['execution']['scheduling']=scheduling_snapshot(store,run_id)
    from .execution_intent import get as get_intent
    row['execution_intent']=get_intent(store,run_id)
    from .plan_readiness import public as readiness,route
    row['plan_readiness']=readiness(store,run_id)
    routing=route(store,run_id)
    row['routing']={k:routing[k] for k in ('stage','capability')} if routing else None
    from .live_activity import current
    row['activity_attempts']=store.query('SELECT id,status,generation FROM execution_attempts WHERE run_id=? ORDER BY generation DESC LIMIT 1000',(run_id,))
    row['activities_at']=store.now()
    row['activities']=current(store,run_id)
    from .public_messages import read
    row['messages']=read(store,run_id)
    from .task_leases import blocked_quota
    row['quota']=blocked_quota(store,run_id)
    from .agent_profiles import public_for_task
    from .harness_switching import latest
    row['agent_profile']=public_for_task(store,run_id)
    row['harness_switch']=latest(store,run_id)
    row['public_notes']=[json.loads(r['payload']) for r in store.query("SELECT payload FROM agent_events WHERE run_id=? AND kind='note' ORDER BY seq",(run_id,))] if not row['messages'] else []
    return row


def event(store, run_id, kind, payload):
    activity=None
    payload={**payload,'event_id':uuid.uuid4().hex}
    from .task_leases import CURRENT
    lease=CURRENT.get()
    if lease and lease.run_id==run_id:
        context={'attempt_id':lease.attempt,'generation':lease.generation,'worker':lease.worker,'step_id':lease.step_id}
        if lease.step_id:
            rows=store.query('SELECT s.spec,s.revision FROM agent_steps s JOIN agent_workflows w ON w.id=s.workflow WHERE w.run_id=? AND s.id=?',(run_id,lease.step_id))
            if not rows:rows=store.query('SELECT s.spec_json AS spec,s.revision FROM run_work_items s JOIN runs w ON w.id=s.run_id WHERE w.id=? AND s.id=?',(run_id,lease.step_id))
            if rows:
                spec=json.loads(rows[0]['spec'])
                context.update(revision=rows[0]['revision'],operation=spec.get('operation','script'),implementation=spec.get('implementation'))
        payload={**payload,'execution':context}
    with store.connect() as db:
        db.execute('INSERT INTO agent_events (run_id,kind,payload,created) VALUES (?,?,?,?)',
                   (run_id, kind, json.dumps(payload, ensure_ascii=False), store.now()))
        if kind=='work':
            from .live_activity import record
            activity=record(db,run_id,payload)
        if kind=='tool':
            from .task_usage import tool
            tool(db,run_id,payload)
            if payload.get('id') and payload.get('status') in ('running','completed','failed'):
                from .live_activity import record
                phase={'commandExecution':'executing','fileChange':'writing','imageView':'inspecting'}.get(payload.get('tool_type'),{'write':'writing','read':'reading','inspect':'inspecting'}.get(payload.get('category'),'executing'))
                label='执行命令' if payload.get('tool_type')=='commandExecution' else payload.get('title','调用工具')
                activity=record(db,run_id,{**payload,'id':'tool:'+(payload.get('execution') or {}).get('attempt_id','')+':'+payload['id'],'actor':payload.get('actor','sandbox_agent'),
                                  'phase':phase,'label':label,'started':payload.get('started',store.now())})
    if activity:
        from .telemetry import emit
        emit('execution.activity',task_id=run_id,activity_id=activity['id'],actor=activity['actor'],phase=activity['phase'],operation=activity['operation'],outcome=activity['status'])
    notice_payload=json.dumps(payload,ensure_ascii=False)
    store.notify_after_commit(lambda:_publish_notice(run_id,kind,json.loads(notice_payload)))


def _publish_notice(run_id,kind,payload):
    from .agent_stream import bus
    from .api_execution import CURRENT_WORK
    transport=bus()
    if CURRENT_WORK.get() is not None:transport.publish_async(run_id,kind,payload)
    else:transport.publish(run_id,kind,payload)


def publish_text_delta(run_id, payload):
    from .agent_stream import bus
    return bus().publish(run_id, 'text_delta', payload)


def save_text_snapshot(store, run_id, text, stream_cursor=None, version=None, phase='idle'):
    """Persist a bounded text snapshot and the exact Redis cursor it includes."""
    if version is not None:
        store.execute('UPDATE agent_turns SET text=?,stream_cursor=?,text_generation=?,text_sequence=?,text_phase=?,updated=? WHERE id=?',
                      (text,stream_cursor or '',version[0],version[1],phase,time.time(),run_id))
    elif stream_cursor is None:
        store.execute('UPDATE agent_turns SET text=?,updated=? WHERE id=?',
                      (text, time.time(), run_id))
    else:
        store.execute('UPDATE agent_turns SET text=?,stream_cursor=?,updated=? WHERE id=?',
                      (text, stream_cursor, time.time(), run_id))


def finish(store, run_id, status, text='', artifacts=None, error=''):
    with store.connect() as db:
        db.execute('BEGIN IMMEDIATE')
        current=db.execute('SELECT status FROM agent_turns WHERE id=?',(run_id,)).fetchone()
        if not current or current['status'] in ('cancelling','cancelled') and status!='cancelled':return False
        if artifacts:
            project=db.execute('SELECT project FROM agent_turns WHERE id=?',(run_id,)).fetchone()['project']
            store.check_storage(project,sum(a.get('size',0) for a in artifacts),replace_turn=run_id)
        db.execute("UPDATE agent_messages SET status='interrupted' WHERE task_id=? AND status='streaming'",(run_id,))
        db.execute("UPDATE agent_turns SET status=?,text=?,artifacts=?,error=?,text_phase='idle',updated=? WHERE id=?",
                   (status, text, json.dumps(artifacts or [], ensure_ascii=False), error, time.time(), run_id))
    store.notify_after_commit(lambda:_publish_notice(run_id,'terminal',{'status':status}))
    return True


def public(row):
    result={key: row[key] for key in ('id','project','mode','prompt','status','text','error','created','updated')}
    from .public_messages import display
    result['text']=display(row)
    result['final_text']=row['text'] if row['status'] in ('succeeded','failed','cancelled') else None
    result['messages']=row.get('messages',[])
    result['text_version']=[row.get('text_generation',0),row.get('text_sequence',0)]
    result['text_phase']=row.get('text_phase','idle')
    result['stream_cursor']=row.get('stream_cursor','')
    result['attachments']=row.get('attachments',[])
    result['execution_intent']=row.get('execution_intent')
    result['plan_readiness']=row.get('plan_readiness',{'state':'none','can_execute':False})
    result['routing']=row.get('routing')
    result['quota']=row.get('quota')
    result['agent_profile']=row.get('agent_profile')
    result['harness_switch']=row.get('harness_switch')
    result['activities']=row.get('activities',[])
    result['activities_at']=row.get('activities_at',0)
    result['activity_attempts']=row.get('activity_attempts',[])
    result['workflow']=row.get('workflow')
    result['execution']=row.get('execution')
    for key in ('canvas_context','canvas_result'):result[key]=row.get(key)
    artifacts=[dict(a,workflow_delivery=a.get('workflow_delivery') is True) for a in row['artifacts']] if row.get('workflow') else row['artifacts']
    result['artifacts']=[dict(a,poster_url=a['url']+'/thumbnail' if a.get('type','').startswith('video/') and a.get('url') else None) for a in order_artifacts(artifacts,row.get('text',''))]
    return result
