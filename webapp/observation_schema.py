"""Transactional, payload-free state history. Rollback also rolls back history."""
import hashlib
from . import telemetry


def initialize(store):
    with store.connect() as db:
        db.executescript('''CREATE TABLE IF NOT EXISTS observation_tasks(
          task_id TEXT PRIMARY KEY,project_id TEXT NOT NULL,trace_id TEXT NOT NULL,
          request_id TEXT NOT NULL,created REAL NOT NULL);
          CREATE INDEX IF NOT EXISTS observation_request ON observation_tasks(request_id);
          CREATE TABLE IF NOT EXISTS observation_audit(
          seq INTEGER PRIMARY KEY AUTOINCREMENT,task_id TEXT NOT NULL,entity TEXT NOT NULL,
          entity_id TEXT NOT NULL,old_state TEXT NOT NULL,new_state TEXT NOT NULL,created REAL NOT NULL);
          CREATE INDEX IF NOT EXISTS observation_audit_task ON observation_audit(task_id,seq);
        ''')
        tables={r['name'] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        specs=[('agent_turns','id','id','status'),('jobs','id','id','status'),
               ('execution_attempts','turn_id','id','status'),('execution_cleanup','task_id','attempt','state'),
               ('execution_external_cleanup','task_id','id','state'),
               ('agent_steps',"(SELECT turn_id FROM agent_workflows WHERE id=NEW.workflow)",'id','status'),
               ('workflow_provider_calls',"COALESCE((SELECT id FROM runs WHERE id=NEW.workflow),(SELECT turn_id FROM agent_workflows WHERE id=NEW.workflow),NEW.workflow)",'request_key','state'),
               ('runs','id','id','status'),
               ('control_jobs',"(SELECT id FROM runs WHERE id=NEW.run_id)",'id','status'),
               ('run_tasks',"(SELECT id FROM runs WHERE id=NEW.run_id)",'id','status'),
               ('planner_runs',"(SELECT id FROM runs WHERE id=NEW.run_id)",'id','status'),
               ('task_results',"(SELECT id FROM runs WHERE id=NEW.run_id)",'id','status'),
               ('workflow_commands',"(SELECT id FROM runs WHERE id=NEW.run_id)",'id','status'),
               ('workflow_decisions',"(SELECT id FROM runs WHERE id=NEW.run_id)",'id','status'),
               ('workflow_reviews',"(SELECT id FROM runs WHERE id=NEW.run_id)",'id','status')]
        existing=set()
        if store.dsn:
            existing={r['tgname'] for r in db.execute("SELECT t.tgname FROM pg_trigger t JOIN pg_class c ON c.oid=t.tgrelid JOIN pg_namespace n ON n.oid=c.relnamespace WHERE NOT t.tgisinternal AND n.nspname=current_schema()")}
        for table,task,key,state in specs:
            if table not in tables:continue
            task_expr=task if task.startswith(('(','COALESCE')) else 'NEW.'+task
            for operation in ('INSERT','UPDATE'):
                name='obs_'+table+'_'+operation.lower()
                old="''" if operation=='INSERT' else 'OLD.'+state
                changed='' if operation=='INSERT' else f' WHEN (OLD.{state} IS DISTINCT FROM NEW.{state})' if store.dsn else f' WHEN OLD.{state} != NEW.{state}'
                clock="CAST(EXTRACT(EPOCH FROM clock_timestamp()) AS DOUBLE PRECISION)" if store.dsn else "(julianday('now')-2440587.5)*86400.0"
                insert=f"INSERT INTO observation_audit(task_id,entity,entity_id,old_state,new_state,created) VALUES ({task_expr},'{table}',NEW.{key},{old},NEW.{state},{clock});"
                if store.dsn:
                    # Already-installed triggers must not take AccessExclusiveLock
                    # on every API/worker restart while read-only scans are active.
                    if name in existing:continue
                    db.execute(f'CREATE OR REPLACE FUNCTION {name}_fn() RETURNS trigger LANGUAGE plpgsql AS $$ BEGIN {insert} RETURN NEW; END $$')
                    # Initialization is serialized by the control-plane lock.
                    db.execute(f'CREATE TRIGGER {name} AFTER {operation} ON {table} FOR EACH ROW{changed} EXECUTE FUNCTION {name}_fn()')
                else:db.execute(f'CREATE TRIGGER IF NOT EXISTS {name} AFTER {operation} ON {table}{changed} BEGIN {insert} END')


def register(db,task_id,project,created):
    ctx=telemetry.CONTEXT.get()
    trace=hashlib.sha256(task_id.encode()).hexdigest()[:32]
    db.execute('INSERT OR IGNORE INTO observation_tasks VALUES (?,?,?,?,?)',
               (task_id,project,trace,ctx.get('request_id',''),created))
    telemetry.emit('task.submission_intent',task_id=task_id,project_id=project,trace_id=trace)
