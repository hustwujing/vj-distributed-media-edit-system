"""Durable consumption evidence. This ledger never authorizes business work."""
from contextlib import contextmanager
from contextvars import ContextVar

CONTROL_TARGET=ContextVar('usage_control_target',default=None)
import hashlib
import json
import time
import uuid
from .usage_protocol import FIELDS, normalize, Receipt


def initialize(db):
    db.executescript('''CREATE TABLE IF NOT EXISTS usage_tasks(
      task_id TEXT PRIMARY KEY,project_id TEXT NOT NULL,run_id TEXT NOT NULL,
      parent_task_id TEXT,coverage TEXT NOT NULL,created REAL NOT NULL);
      CREATE INDEX IF NOT EXISTS usage_run ON usage_tasks(run_id,task_id);
      CREATE TABLE IF NOT EXISTS usage_calls(
      seq INTEGER PRIMARY KEY AUTOINCREMENT,call_id TEXT NOT NULL UNIQUE,task_id TEXT NOT NULL,
      attempt_id TEXT NOT NULL,step_id TEXT NOT NULL,revision INTEGER,category TEXT NOT NULL,
      operation TEXT NOT NULL,status TEXT NOT NULL,started REAL NOT NULL,finished REAL,
      input_tokens BIGINT,output_tokens BIGINT,total_tokens BIGINT,cached_tokens BIGINT,reasoning_tokens BIGINT,
      response_id TEXT NOT NULL DEFAULT '',receipt TEXT NOT NULL DEFAULT 'missing',
      error_code TEXT NOT NULL DEFAULT '',parent_call_id TEXT);
      CREATE INDEX IF NOT EXISTS usage_task_calls ON usage_calls(task_id,seq);
      CREATE INDEX IF NOT EXISTS usage_attempt_calls ON usage_calls(attempt_id);
    ''')
    # Old turns have explicit plan ancestry only. Never merge by prompt/project.
    for row in db.execute('SELECT id,project,created,plan_id FROM agent_turns WHERE id NOT IN (SELECT task_id FROM usage_tasks) ORDER BY created').fetchall():
        attach(db,row['id'],row['project'],row['created'],row['plan_id'],coverage='historical')
    db.execute("INSERT INTO usage_tasks(task_id,project_id,run_id,coverage,created) SELECT id,project,id,'historical',created FROM jobs WHERE id NOT IN (SELECT task_id FROM usage_tasks)")


def attach(db,task,project,created,parent=None,coverage='recorded'):
    run=task
    if parent:
        row=db.execute('SELECT project_id,run_id FROM usage_tasks WHERE task_id=?',(parent,)).fetchone()
        if not row or row['project_id']!=project:raise ValueError('关联任务不属于当前项目')
        run=row['run_id']
    db.execute('INSERT OR IGNORE INTO usage_tasks(task_id,project_id,run_id,parent_task_id,coverage,created) VALUES (?,?,?,?,?,?)',
               (task,project,run,parent,coverage,created))


def start(db,task,category,operation,*,call_id=None,attempt='',step='',revision=None,parent=None,started=None):
    call_id=call_id or uuid.uuid4().hex
    if step and category!='workflow' and parent is None:
        row=db.execute("SELECT call_id FROM usage_calls WHERE task_id=? AND step_id=? AND category='workflow' AND status='running' ORDER BY seq DESC LIMIT 1",(task,step)).fetchone()
        parent=row['call_id'] if row else None
    db.execute('''INSERT OR IGNORE INTO usage_calls(call_id,task_id,attempt_id,step_id,revision,category,operation,status,started,parent_call_id)
      VALUES (?,?,?,?,?,?,?,'running',?,?)''',(call_id,task,attempt,step,revision,category,(operation or 'script')[:200],started or time.time(),parent))
    from .telemetry import emit
    emit('usage.call_intent',call_id=call_id,task_id=task,attempt_id=attempt or None,step_id=step or None,operation=operation or 'script')
    return call_id


def complete(db,task,call,status,usage=None,response_id='',error='',finished=None):
    if status not in ('succeeded','failed','cancelled','incomplete','unknown','running'):raise ValueError('Invalid usage state')
    row=db.execute('SELECT * FROM usage_calls WHERE call_id=? AND task_id=?',(call,task)).fetchone()
    if not row:raise ValueError('Receipt has no matching prior call')
    from .usage_protocol import number
    values={key:number((usage or {}).get(key)) for key in FIELDS}
    known=any(values[key] is not None for key in FIELDS)
    # Partial receipts can be enriched. Contradictory non-null values are never
    # overwritten or added; they flag a discrepancy for inspection.
    conflict=any(row[key] is not None and values[key] is not None and row[key]!=values[key] for key in FIELDS)
    if known:
        merged=[row[key] if row[key] is not None else values[key] for key in FIELDS]
        receipt='conflict' if conflict or row['receipt']=='conflict' else 'reported'
        db.execute('UPDATE usage_calls SET input_tokens=?,output_tokens=?,total_tokens=?,cached_tokens=?,reasoning_tokens=?,response_id=?,receipt=? WHERE call_id=?',
                   (*merged,row['response_id'] or response_id[:200],receipt,call))
    db.execute("UPDATE usage_calls SET status=?,finished=?,error_code=? WHERE call_id=? AND status IN ('running','unknown')",
               (status,finished or time.time(),error[:80],call))
    from .telemetry import emit
    emit('usage.call_receipt',call_id=call,task_id=task,attempt_id=row['attempt_id'] or None,outcome=status,reason='reported' if known else 'missing')


def tool(db,task,payload):
    identity=payload.get('id');context=payload.get('execution') or {}
    if not identity:return
    attempt=context.get('attempt_id','')
    call=hashlib.sha256(f'tool:{task}:{attempt}:{identity}'.encode()).hexdigest()
    start(db,task,'tool',payload.get('tool_type') or payload.get('category','tool'),call_id=call,attempt=attempt,
          step=context.get('step_id') or '',revision=context.get('revision'),started=payload.get('started'))
    status=payload.get('status')
    if status!='running':complete(db,task,call,'failed' if status=='failed' else 'succeeded',finished=payload.get('completed'))


@contextmanager
def model_call(lease,model,actor='host',observer=None):
    """Fail closed before submission; receipt-only transactions may outlive lease."""
    observer=observer or Receipt();call=None;error='';state='unknown'
    if lease:
        with lease.store.connect() as db:
            call=start(db,lease.turn_id,'llm',model,attempt=lease.attempt,step=lease.step_id or '')
    from .live_activity import span
    try:
        with span(actor,'model_wait','等待模型响应',model) as activity:
            observer.on_activity=activity.model_phase
            yield observer
        state=observer.status or 'unknown'
    except BaseException as exc:
        error=type(exc).__name__
        state='cancelled' if error in ('LeaseLost','TaskLimitReached','BrokenPipeError','ConnectionResetError') else 'unknown'
        raise
    finally:
        observer.finish()
        if call:
            from .task_lifecycle import administrative
            # Separate connection avoids a stale business fence and serializes by
            # call row on PostgreSQL. It may only update an existing call receipt.
            from .store import Store
            from .execution_metrics import measurement_connection
            ledger=Store(lease.store.root,timeout=2,initialize_schema=False,dsn=lease.store.dsn)
            try:
                with administrative(),measurement_connection(ledger) as db:
                    if ledger.dsn:db.execute('SELECT call_id FROM usage_calls WHERE call_id=? FOR UPDATE',(call,)).fetchone()
                    complete(db,lease.turn_id,call,observer.status or state,observer.usage,observer.response_id,error)
            except Exception as exc:
                from .telemetry import emit
                emit('usage.receipt_failed','ERROR',exc=exc,task_id=lease.turn_id,attempt_id=lease.attempt)


def numeric_tree(value,depth=0):
    """Export measurements, never arbitrary provider strings or signed URLs."""
    if depth>4:return None
    if type(value) in (int,float):
        import math
        return value if math.isfinite(value) and value>=0 else None
    if isinstance(value,dict):
        import re
        return {k:numeric_tree(v,depth+1) for k,v in list(value.items())[:80]
                if isinstance(k,str) and re.fullmatch(r'[A-Za-z_][A-Za-z0-9_]{0,60}',k) and isinstance(v,(dict,int,float)) and not isinstance(v,bool)}
    return None


def snapshot(store,task,scope='task',after=0,limit=50,provider_after=0):
    existing=getattr(store._connections,'db',None)
    if store.dsn and existing is None:
        import psycopg
        from psycopg.rows import dict_row
        from .database import PostgresConnection
        from types import SimpleNamespace
        with psycopg.connect(store.dsn,connect_timeout=5,row_factory=dict_row) as raw:
            raw.isolation_level=psycopg.IsolationLevel.REPEATABLE_READ
            raw.read_only=True
            raw.execute("SELECT set_config('statement_timeout','5000',true)")
            db=PostgresConnection(raw)
            from .task_leases import now
            reader=SimpleNamespace(query=lambda sql,args=():[dict(r) for r in db.execute(sql,args).fetchall()],now=lambda:now(db))
            return _snapshot(reader,task,scope,after,limit,provider_after)
    with store.connect(readonly=True) as db:
        if existing is None:db.execute('BEGIN')
        return _snapshot(store,task,scope,after,limit,provider_after)


def _snapshot(store,task,scope,after,limit,provider_after):
    if scope not in ('task','run','project'):raise ValueError('Invalid usage scope')
    mapping=store.query('SELECT * FROM usage_tasks WHERE task_id=?',(task,))
    if not mapping:raise ValueError('Unknown task')
    root=mapping[0]
    members=store.query('SELECT * FROM usage_tasks WHERE run_id=? AND project_id=? ORDER BY created',(root['run_id'],root['project_id'])) if scope=='run' else mapping
    if scope=='project':members=store.query('SELECT * FROM usage_tasks WHERE project_id=? ORDER BY created',(root['project_id'],))
    ids=[x['task_id'] for x in members];marks=','.join('?' for _ in ids)
    # Aggregates have no detail pagination limit. No hidden rolling counter.
    groups=store.query(f'''SELECT category,effective_status AS status,COUNT(*) AS calls FROM (SELECT c.category,CASE WHEN c.status='running' AND a.status IN ('expired','interrupted','finished') THEN 'unknown' ELSE c.status END AS effective_status FROM usage_calls c LEFT JOIN execution_attempts a ON a.id=c.attempt_id WHERE c.task_id IN ({marks})) AS states GROUP BY category,effective_status''',ids)
    counts={k:{'calls':0,'states':{}} for k in ('llm','tool','workflow','provider_submit','provider_poll','provider_collect')}
    for row in groups:
        entry=counts[row['category']];entry['calls']+=row['calls'];entry['states'][row['status']]=row['calls']
    tokens={}
    for field in FIELDS:
        row=store.query(f"SELECT SUM({field}) AS known,COUNT({field}) AS reported FROM usage_calls WHERE category='llm' AND task_id IN ({marks})",ids)[0]
        tokens[field]={'known':int(row['known']) if row['known'] is not None else None,'reported_calls':row['reported'],'missing_calls':counts['llm']['calls']-row['reported']}
    calculated=store.query(f"""SELECT
        SUM(CASE WHEN total_tokens IS NOT NULL THEN total_tokens
            WHEN input_tokens IS NOT NULL AND output_tokens IS NOT NULL THEN input_tokens+output_tokens ELSE NULL END) AS known,
        SUM(CASE WHEN total_tokens IS NULL AND input_tokens IS NOT NULL AND output_tokens IS NOT NULL THEN 1 ELSE 0 END) AS derived,
        SUM(CASE WHEN total_tokens IS NULL AND (input_tokens IS NULL OR output_tokens IS NULL) THEN 1 ELSE 0 END) AS missing,
        SUM(CASE WHEN total_tokens IS NOT NULL AND input_tokens IS NOT NULL AND output_tokens IS NOT NULL AND total_tokens<>input_tokens+output_tokens THEN 1 ELSE 0 END) AS inconsistent
        FROM usage_calls WHERE category='llm' AND task_id IN ({marks})""",ids)[0]
    computed_total={'known':int(calculated['known']) if calculated['known'] is not None else None,
        'derived_calls':int(calculated['derived'] or 0),'missing_calls':int(calculated['missing'] or 0),
        'inconsistent_calls':int(calculated['inconsistent'] or 0)}
    details=store.query(f'''SELECT c.*,a.status AS attempt_status FROM usage_calls c LEFT JOIN execution_attempts a ON a.id=c.attempt_id
        WHERE c.task_id IN ({marks}) AND c.seq>? ORDER BY c.seq LIMIT ?''',(*ids,after,limit+1))
    for row in details:
        if row['status']=='running' and row['attempt_status'] in ('expired','interrupted','finished'):row['status']='unknown'
    incomplete=store.query(f'''SELECT COUNT(*) AS n FROM usage_calls c JOIN execution_attempts a ON a.id=c.attempt_id
        WHERE c.task_id IN ({marks}) AND c.status='running' AND a.status IN ('expired','interrupted','finished')''',ids)[0]['n']
    retries=store.query(f"SELECT COALESCE(SUM(n-1),0) AS n FROM (SELECT COUNT(*) AS n FROM usage_calls WHERE category='workflow' AND task_id IN ({marks}) GROUP BY task_id,step_id,revision) AS attempts",ids)[0]['n']
    repairs=store.query(f'SELECT COALESCE(SUM(s.repair_count),0) AS n FROM agent_steps s JOIN agent_workflows w ON w.id=s.workflow WHERE w.turn_id IN ({marks})',ids)[0]['n']
    providers=store.query(f'''SELECT p.request_key,p.operation,p.state,p.usage,p.cost,p.poll_count,p.network_seconds,p.step,p.revision,
        p.attempt_id,p.created,p.updated FROM workflow_provider_calls p LEFT JOIN agent_workflows w ON w.id=p.workflow
        WHERE COALESCE(w.turn_id,p.workflow) IN ({marks}) ORDER BY p.created''',ids)
    for row in providers:
        row['usage']=numeric_tree(json.loads(row['usage']))
        cost=json.loads(row['cost']);row['cost']=None
        if isinstance(cost,dict) and isinstance(cost.get('currency'),str) and len(cost['currency'])==3 and cost['currency'].isalpha():
            amount=numeric_tree(cost.get('amount'))
            if isinstance(amount,(int,float)):row['cost']={'amount':amount,'currency':cost['currency'].upper()}
    quotas=store.query(f'SELECT category,SUM(used) AS used FROM execution_budgets WHERE turn_id IN ({marks}) GROUP BY category',ids)
    phases=store.query(f'SELECT phase,SUM(seconds) AS seconds,SUM(calls) AS calls FROM execution_phase_metrics WHERE task_id IN ({marks}) GROUP BY phase',ids)
    historical=[x['task_id'] for x in members if x['coverage']=='historical']
    historical_tools=0
    if historical:
        hm=','.join('?' for _ in historical)
        events=store.query(f"SELECT turn_id,payload FROM agent_events WHERE kind='tool' AND turn_id IN ({hm})",historical)
        historical_tools=len({(e['turn_id'],(p.get('execution') or {}).get('attempt_id',''),p.get('id')) for e in events if (p:=json.loads(e['payload'])).get('id')})
    attempts=store.query(f"SELECT COUNT(*) AS dispatches,COUNT(DISTINCT CASE WHEN runtime<>'' THEN id END) AS sandboxes FROM execution_attempts WHERE turn_id IN ({marks})",ids)[0]
    model_groups=store.query(f"SELECT CASE WHEN c.operation LIKE 'review:%' THEN 'proposal_review' WHEN a.runtime<>'' THEN 'sandbox_agent' ELSE 'host_or_light' END AS purpose,COUNT(*) AS calls FROM usage_calls c LEFT JOIN execution_attempts a ON a.id=c.attempt_id WHERE c.category='llm' AND c.task_id IN ({marks}) GROUP BY 1",ids)
    for call in details:
        if call['category']=='llm':
            call['computed_total_tokens']=call['total_tokens'] if call['total_tokens'] is not None else call['input_tokens']+call['output_tokens'] if call['input_tokens'] is not None and call['output_tokens'] is not None else None
            call['total_source']='reported' if call['total_tokens'] is not None else 'calculated' if call['computed_total_tokens'] is not None else 'missing'
        call['duration_seconds']=max(0,call['finished']-call['started']) if call['finished'] else None
    from .planning_budget import snapshot as planning
    planning_state=planning(store,task)
    harnesses=[]
    if store.query("SELECT name FROM sqlite_master WHERE name='agent_attempt_profiles'"):
        profiles=store.query(f'''SELECT p.digest,p.configuration,COUNT(DISTINCT p.attempt_id) AS attempts,
          COUNT(c.call_id) AS model_calls FROM agent_attempt_profiles p LEFT JOIN usage_calls c
          ON c.attempt_id=p.attempt_id AND c.category='llm' WHERE p.task_id IN ({marks})
          GROUP BY p.digest,p.configuration''',ids)
        for entry in profiles:
            config=json.loads(entry['configuration'])
            harnesses.append({'digest':entry['digest'],'profile_name':config['name'],'harness':config['harness'],'model':config['model'],
                              'attempts':entry['attempts'],'model_calls':entry['model_calls']})
    return {'task_id':task,'run_id':root['run_id'],'scope':scope,'members':members,'counts':counts,'tokens':tokens,'computed_total':computed_total,'execution_counts':attempts,'model_purposes':model_groups,'planning_budget':planning_state,'harness_runs':harnesses,
            'workflow_retries':int(retries),'repairs':repairs,'llm_retries':None,'unfinished_calls_on_finished_attempts':incomplete,
            'provider':{'submissions':len(providers),'polls':sum(p['poll_count'] for p in providers),
                        'reported_usage':sum(p['usage'] is not None for p in providers),'reported_cost':sum(p['cost'] is not None for p in providers),
                        'submission_intents':len(providers),'actual_submissions':counts['provider_submit']['calls'],
                        'actual_polls':counts['provider_poll']['calls'],
                        'details':providers[provider_after:provider_after+limit],
                        'next_after':provider_after+limit if len(providers)>provider_after+limit else None,
                        'details_truncated':len(providers)>provider_after+limit},
            'quota_consumption':quotas,'phase_wall_time':phases,'cpu_seconds':None,'gpu_seconds':None,
            'coverage':{'historical_tasks':historical,'historical_tools_from_events':historical_tools,
                        'main_llm_tokens_before_upgrade':'unavailable' if historical else 'not_applicable'},
            'calls':details[:limit],'next_after':details[limit-1]['seq'] if len(details)>limit else None}


@contextmanager
def control_target(store,task):
    from types import SimpleNamespace
    token=CONTROL_TARGET.set(SimpleNamespace(store=store,turn_id=task,attempt='',step_id=''))
    try:yield
    finally:CONTROL_TARGET.reset(token)
