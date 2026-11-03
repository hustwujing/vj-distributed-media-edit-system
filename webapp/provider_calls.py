"""Auditable provider intent, identity and usage; unknown prices stay unknown."""
import json
import time
from contextlib import contextmanager
from .egress_policy import scrub,secrets


def initialize(db):
    db.executescript('''CREATE TABLE IF NOT EXISTS workflow_provider_calls(
      request_key TEXT PRIMARY KEY,workflow TEXT NOT NULL,step TEXT NOT NULL,revision INTEGER NOT NULL,
      attempt INTEGER NOT NULL,provider TEXT NOT NULL,operation TEXT NOT NULL,state TEXT NOT NULL,
      external_id TEXT NOT NULL DEFAULT '',inputs TEXT NOT NULL,usage TEXT NOT NULL DEFAULT 'null',
      cost TEXT NOT NULL DEFAULT 'null',created REAL NOT NULL,updated REAL NOT NULL
    );''')
    db.execute('CREATE TABLE IF NOT EXISTS workflow_transcript_cache(project TEXT NOT NULL,cache_key TEXT NOT NULL,path TEXT NOT NULL,sha256 TEXT NOT NULL,created REAL NOT NULL,PRIMARY KEY(project,cache_key))')
    columns={r['name'] for r in db.execute('PRAGMA table_info(workflow_provider_calls)')}
    for name,definition in {'parameters':"TEXT NOT NULL DEFAULT '{}'",'attempt_id':"TEXT NOT NULL DEFAULT ''",'generation':'INTEGER NOT NULL DEFAULT 0'}.items():
        if name not in columns:db.execute(f'ALTER TABLE workflow_provider_calls ADD COLUMN {name} {definition}')
    if 'implementation' not in columns:db.execute("ALTER TABLE workflow_provider_calls ADD COLUMN implementation TEXT NOT NULL DEFAULT '{}' ")
    if 'network_seconds' not in columns:db.execute('ALTER TABLE workflow_provider_calls ADD COLUMN network_seconds REAL NOT NULL DEFAULT 0')
    if 'poll_count' not in columns:db.execute('ALTER TABLE workflow_provider_calls ADD COLUMN poll_count INTEGER NOT NULL DEFAULT 0')
    if 'result' not in columns:db.execute("ALTER TABLE workflow_provider_calls ADD COLUMN result TEXT NOT NULL DEFAULT '[]'")
    if 'failure' not in columns:db.execute("ALTER TABLE workflow_provider_calls ADD COLUMN failure TEXT NOT NULL DEFAULT '{}'")
    if 'authorization' not in columns:db.execute("ALTER TABLE workflow_provider_calls ADD COLUMN authorization TEXT NOT NULL DEFAULT '{}'")
    if 'cookies' not in {r['name'] for r in db.execute('PRAGMA table_info(workflow_provider_calls)')}:
        db.execute("ALTER TABLE workflow_provider_calls ADD COLUMN cookies TEXT NOT NULL DEFAULT '{}' ")


def save_session(store,w,s,session):
    cookies=session.cookies.get_dict()
    store.execute('UPDATE workflow_provider_calls SET cookies=? WHERE request_key=?',
        (json.dumps(cookies),w['id']+':'+s['id']+':'+str(s['revision'])))


def restore_session(store,w,s,session):
    rows=store.query('SELECT cookies FROM workflow_provider_calls WHERE request_key=?',
        (w['id']+':'+s['id']+':'+str(s['revision']),))
    if rows:session.cookies.update(json.loads(rows[0]['cookies']))


def intent(store,w,s,provider,operation,implementation=None):
    from . import operations
    from .provider_metadata import metadata
    selected=operations.for_step(s['spec']) if s.get('spec',{}).get('kind')=='operation' else operations.get(operation)
    if not selected.external_side_effects:raise ValueError('Local operation cannot submit a provider call')
    key=w['id']+':'+s['id']+':'+str(s['revision'])
    with store.connect() as db:
        db.execute('BEGIN IMMEDIATE')
        row=db.execute('SELECT state FROM workflow_provider_calls WHERE request_key=?',(key,)).fetchone()
        if row:raise ValueError('此修订已有外部提交记录，不允许重复提交')
        from .task_leases import CURRENT
        lease=CURRENT.get()
        if w.get('dynamic'):
            from .workflow_graph import get as graph_get
            dynamic=graph_get(store,w['id'])
            if not lease or not dynamic or lease.run_id!=dynamic['id'] or lease.step_id!=s['id']:
                raise ValueError('Provider call is outside the leased graph task')
            task=next(t for t in dynamic['tasks'] if t['id']==s['id'])
            from .workflow_graph import authority_for
            scope=authority_for(store,dynamic,task)
            if not dynamic['scope_confirmed']:raise ValueError('Business scope is not confirmed')
            if task['spec'].get('operation')!=operation or operation not in scope['allowed_operations']:
                raise ValueError('Provider operation was not authorized')
            from .workflow_graph import digest
            if scope['operation_policy_digests'].get(operation)!=digest(metadata(selected)):
                raise ValueError('Provider policy changed after authorization')
            lease.consume('provider:'+selected.budget_group,dynamic['authorization']['provider_call_budget'].get(selected.budget_group,0))
        if lease and not w.get('dynamic') and w.get('origin')!='direct':
            import os
            category='video' if operation.startswith('video.') else 'transcribe' if operation=='audio.transcribe' else 'image'
            limit=int(os.environ.get('VIDEO_USE_TASK_VIDEO_CALLS','2')) if category=='video' else 50 if category=='transcribe' else 4
            lease.consume(category,limit)
        step=db.execute('SELECT attempts FROM run_tasks WHERE run_id=? AND id=?' if w.get('dynamic') else 'SELECT attempts FROM agent_steps WHERE workflow=? AND id=?',(w['id'],s['id'])).fetchone()
        attempt=max(1,step['attempts']) if step else s['attempts']+1
        encoded=scrub(json.dumps(s.get('bindings',{}),ensure_ascii=False),secrets())
        db.execute('INSERT INTO workflow_provider_calls(request_key,workflow,step,revision,attempt,provider,operation,state,inputs,created,updated) VALUES (?,?,?,?,?,?,?,?,?,?,?)',
            (key,w['id'],s['id'],s['revision'],attempt,provider,operation,'submitting',encoded,store.now(),store.now()))
        from .execution_intent import get
        authorization={'origin':'manual' if w.get('manual') else w.get('origin','workflow'),'plan_id':w.get('plan_id'),'confirmed_intent':get(store,w['run_id'])}
        if w.get('dynamic'):
            authorization={'origin':'dynamic_workflow','run_id':dynamic['id'],
                'authorization_version':task['authorization_version'],'budget_authorization_version':dynamic['authorization_version'],'content_fingerprint':scope['content_fingerprint'],
                'operation':operation,'budget_group':selected.budget_group,'provider_call_budget':dynamic['authorization']['provider_call_budget'].get(selected.budget_group,0)}
        db.execute('UPDATE workflow_provider_calls SET authorization=? WHERE request_key=?',(json.dumps(authorization),key))
        from .operation_lifecycle import for_operation
        binding={**(implementation or {}),'implementation_id':for_operation(selected).id,**metadata(selected)}
        db.execute('UPDATE workflow_provider_calls SET implementation=? WHERE request_key=?',(json.dumps(binding),key))
        db.execute('UPDATE workflow_provider_calls SET parameters=?,attempt_id=?,generation=? WHERE request_key=?',
                   (scrub(json.dumps(s.get('spec',{}).get('parameters',{}),ensure_ascii=False),secrets()),lease.attempt if lease else '',lease.generation if lease else 0,key))
    from .telemetry import emit
    emit('provider.submission_intent',implementation=binding['implementation_id'],request_key=key,provider=provider,operation=operation,step_id=s['id'],revision=s['revision'])
    return key


def implementation(store,w,s):
    rows=store.query('SELECT implementation FROM workflow_provider_calls WHERE request_key=?',(w['id']+':'+s['id']+':'+str(s['revision']),))
    return json.loads(rows[0]['implementation']) if rows else {}


def update(store,w,s,state,external_id='',record=None,cookies=None):
    record=record or {}
    from .telemetry import emit
    emit('provider.state_observed',request_key=w['id']+':'+s['id']+':'+str(s['revision']),step_id=s['id'],revision=s['revision'],external_id=external_id,new_state=state)
    key=w['id']+':'+s['id']+':'+str(s['revision'])
    from .task_leases import LeaseLost
    from .task_lifecycle import TaskLimitReached
    try:
        with store.connect():
            store.execute('UPDATE workflow_provider_calls SET state=?,external_id=CASE WHEN ?<>\'\' THEN ? ELSE external_id END,usage=CASE WHEN ?<>\'null\' THEN ? ELSE usage END,cost=CASE WHEN ?<>\'null\' THEN ? ELSE cost END,updated=? WHERE request_key=?',
                (state,external_id,external_id,json.dumps(record.get('usage')),json.dumps(record.get('usage')),json.dumps(record.get('cost')),json.dumps(record.get('cost')),store.now(),key))
            if cookies is not None:store.execute('UPDATE workflow_provider_calls SET cookies=? WHERE request_key=?',(json.dumps(cookies),key))
            if state in ('failed','cancelled') and (record.get('error') or record.get('error_code') or record.get('message')):
                from .generation_errors import failure_info
                store.execute('UPDATE workflow_provider_calls SET failure=? WHERE request_key=?',
                    (json.dumps(failure_info({**record,'status':state}),ensure_ascii=False),key))
    except (LeaseLost,TaskLimitReached):
        # Receipt-only authority after revocation: record the external side effect
        # and arrange cleanup. This path never changes steps or publishes results.
        late_receipt(store,w['run_id'],key,state,external_id,record,cookies=cookies)
        raise


def late_receipt(store,task_id,key,state,external_id,record,cookies=None):
    from .task_lifecycle import administrative
    import re
    if external_id and not re.fullmatch(r'[A-Za-z0-9_-]{1,100}',external_id):raise ValueError('Invalid late provider ID')
    if state not in ('waiting','running','succeeded','failed','cancelled','rejected','submission_unknown'):
        raise ValueError('Unsupported provider receipt state')
    with administrative(),store.connect() as db:
        db.execute('BEGIN IMMEDIATE')
        row=db.execute('SELECT * FROM workflow_provider_calls WHERE request_key=?',(key,)).fetchone()
        if not row:raise ValueError('Receipt has no prior submission intent')
        if row['workflow']!=task_id:
            workflow=db.execute('SELECT run_id FROM agent_workflows WHERE id=? UNION ALL SELECT id FROM runs WHERE id=?',(row['workflow'],row['workflow'])).fetchone()
            if not workflow or workflow['run_id']!=task_id:raise ValueError('Receipt belongs to another task')
        if row['external_id'] and external_id and row['external_id']!=external_id:
            raise ValueError('Conflicting late provider ID')
        identifier=external_id or row['external_id']
        terminal=row['state'] if row['state'] in ('succeeded','failed','cancelled','rejected') else state
        db.execute('UPDATE workflow_provider_calls SET state=?,external_id=?,usage=?,cost=?,updated=? WHERE request_key=?',
                   (terminal,identifier,json.dumps(record['usage']) if record.get('usage') is not None else row['usage'],json.dumps(record['cost']) if record.get('cost') is not None else row['cost'],store.now(),key))
        if cookies is not None:db.execute('UPDATE workflow_provider_calls SET cookies=? WHERE request_key=?',(json.dumps(cookies),key))
        if state in ('failed','cancelled') and (record.get('error') or record.get('error_code') or record.get('message')):
            from .generation_errors import failure_info
            db.execute('UPDATE workflow_provider_calls SET failure=? WHERE request_key=?',
                (json.dumps(failure_info({**record,'status':state}),ensure_ascii=False),key))
        cleanup_state='complete' if terminal in ('succeeded','failed','cancelled','rejected') else 'pending' if identifier else 'unknown'
        db.execute('''INSERT INTO execution_external_cleanup(id,task_id,provider,external_id,state,error)
          VALUES (?,?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET external_id=excluded.external_id,
          state=excluded.state,phase='cancel',next_retry=0
          WHERE execution_external_cleanup.state IN ('unknown','unconfirmed')''',
          (key,task_id,row['provider'],identifier,cleanup_state,'Late receipt after execution revocation'))


@contextmanager
def request_time(store,w,s,phase):
    started=time.monotonic()
    from .task_usage import start,complete
    from .task_leases import CURRENT
    from .task_lifecycle import administrative
    lease=CURRENT.get();state='succeeded'
    with store.connect() as db:
        call=start(db,w['run_id'],'provider_'+phase,s.get('spec',{}).get('operation') or phase,
                   attempt=lease.attempt if lease else '',step=s['id'],revision=s['revision'])
    try:yield
    except BaseException:
        state='unknown';raise
    finally:
        # Revocation cannot erase an attempted network request. Receipt-only write.
        with administrative(),store.connect() as db:
            complete(db,w['run_id'],call,state)
        from .task_leases import LeaseLost
        from .task_lifecycle import TaskLimitReached
        try:
            store.execute('UPDATE workflow_provider_calls SET network_seconds=network_seconds+?,poll_count=poll_count+? WHERE request_key=?',
                (time.monotonic()-started,int(phase=='poll'),w['id']+':'+s['id']+':'+str(s['revision'])))
        except (LeaseLost,TaskLimitReached):pass
