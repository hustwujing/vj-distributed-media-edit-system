"""Durable deadlines, reserved compute time and independently tracked cleanup."""
from contextlib import contextmanager
import os


class TaskLimitReached(RuntimeError):
    def __init__(self, reason):
        self.reason=reason
        super().__init__({'deadline':'任务总时限已到','budget':'任务累计执行预算已用完','security':'任务已因安全策略停止'}.get(reason,'任务已停止'))


@contextmanager
def administrative():
    from .task_leases import CURRENT
    token=CURRENT.set(None)
    try:yield
    finally:CURRENT.reset(token)


def clock(db):
    from .task_leases import now
    return now(db)


def initialize(db):
    db.executescript('''
      CREATE TABLE IF NOT EXISTS execution_limits(
        task_id TEXT PRIMARY KEY, deadline_at REAL NOT NULL, budget_seconds REAL NOT NULL,
        used_seconds REAL NOT NULL DEFAULT 0, termination_reason TEXT NOT NULL DEFAULT ''
      );
      CREATE TABLE IF NOT EXISTS execution_time_slices(
        attempt TEXT PRIMARY KEY, task_id TEXT NOT NULL, accounted_at REAL NOT NULL,
        reserved REAL NOT NULL DEFAULT 0, computing INTEGER NOT NULL DEFAULT 0
      );
      CREATE TABLE IF NOT EXISTS execution_cleanup(
        attempt TEXT PRIMARY KEY, task_id TEXT NOT NULL, node TEXT NOT NULL,
        state TEXT NOT NULL, started REAL, deadline_at REAL, updated REAL NOT NULL,
        retries INTEGER NOT NULL DEFAULT 0, next_retry REAL NOT NULL DEFAULT 0,
        error TEXT NOT NULL DEFAULT ''
      );
      CREATE TABLE IF NOT EXISTS execution_security_events(
        id INTEGER PRIMARY KEY AUTOINCREMENT,task_id TEXT NOT NULL,category TEXT NOT NULL,created REAL NOT NULL
      );
      CREATE TABLE IF NOT EXISTS execution_external_cleanup(
        id TEXT PRIMARY KEY,task_id TEXT NOT NULL,provider TEXT NOT NULL,external_id TEXT NOT NULL,
        state TEXT NOT NULL,phase TEXT NOT NULL DEFAULT 'cancel',attempts INTEGER NOT NULL DEFAULT 0,
        next_retry REAL NOT NULL DEFAULT 0,lease_until REAL NOT NULL DEFAULT 0,error TEXT NOT NULL DEFAULT ''
      );
      CREATE TABLE IF NOT EXISTS execution_cancel_requests(
        task_id TEXT PRIMARY KEY,request_id TEXT NOT NULL,scope TEXT NOT NULL,
        reason TEXT NOT NULL,control_version INTEGER NOT NULL,created REAL NOT NULL
      );
    ''')
    columns={r['name'] for r in db.execute('PRAGMA table_info(execution_external_cleanup)')}
    for name,definition in {'deadline_at':'REAL NOT NULL DEFAULT 0','generation':'INTEGER NOT NULL DEFAULT 0',
                            'owner':"TEXT NOT NULL DEFAULT ''",'reconciliations':'INTEGER NOT NULL DEFAULT 0'}.items():
        if name not in columns:db.execute(f'ALTER TABLE execution_external_cleanup ADD COLUMN {name} {definition}')
    from .execution_metrics import initialize as initialize_metrics
    initialize_metrics(db)


def cancel_task(store,task_id,reason='user'):
    """Atomically revoke business permission and create independently owned cleanup."""
    from .workflow_graph import get as graph_get,command
    if graph_get(store,task_id):
        result=command(store,task_id,'user','cancel',{'reason':reason})
        return {'cancelled':False,'status':'cancelling','request_id':result['id']}
    import uuid
    with store.connect() as db:
        db.execute('BEGIN IMMEDIATE')
        task=db.execute('SELECT status FROM execution_subjects WHERE id=?',(task_id,)).fetchone()
        if not task:raise ValueError('Unknown task')
        if task['status'] in ('succeeded','failed'):return {'cancelled':False,'status':task['status']}
        request=db.execute('SELECT request_id FROM execution_cancel_requests WHERE task_id=?',(task_id,)).fetchone()
        if not request:
            identifier=uuid.uuid4().hex
            db.execute('INSERT INTO execution_cancel_requests VALUES (?,?,?,?,?,?)',(task_id,identifier,'task',reason,1,clock(db)))
        else:identifier=request['request_id']
        timestamp=clock(db)
        for table in ('agent_turns','jobs'):
            db.execute(f"UPDATE {table} SET status='cancelled',updated=? WHERE id=? AND status NOT IN ('succeeded','failed')",(timestamp,task_id))
        db.execute("UPDATE agent_workflows SET status='cancelled',updated=? WHERE run_id=? AND status NOT IN ('succeeded','failed')",(timestamp,task_id))
        request_cleanup(db,task_id)
        db.execute("UPDATE agent_steps SET status='cancelled',updated=? WHERE workflow IN (SELECT id FROM agent_workflows WHERE run_id=?) AND status<>'succeeded'",(timestamp,task_id))
        for table in ('workflow_agent_reviews','workflow_user_requests'):
            db.execute(f"UPDATE {table} SET status='cancelled' WHERE workflow IN (SELECT id FROM agent_workflows WHERE run_id=?) AND status IN ('pending','running','staged')",(task_id,))
        return {'cancelled':True,'status':'cancelled','request_id':identifier}


def ensure(db,task_id,created=None):
    created=clock(db) if created is None else created
    lifetime=max(1,float(os.environ.get('VIDEO_USE_TASK_TTL_SECONDS','86400')))
    budget=max(1,float(os.environ.get('VIDEO_USE_TASK_BUDGET_SECONDS','7200')))
    db.execute('INSERT OR IGNORE INTO execution_limits(task_id,deadline_at,budget_seconds) VALUES (?,?,?)',
               (task_id,created+lifetime,budget))
    return db.execute('SELECT * FROM execution_limits WHERE task_id=?',(task_id,)).fetchone()


def check(db,task_id,attempt=None):
    limit=ensure(db,task_id)
    now=clock(db)
    reason=limit['termination_reason']
    if reason or now>=limit['deadline_at']:raise TaskLimitReached(reason or 'deadline')
    if attempt:
        slot=db.execute('SELECT * FROM execution_time_slices WHERE attempt=?',(attempt,)).fetchone()
        if slot and slot['computing'] and now>=slot['accounted_at']+slot['reserved']:
            raise TaskLimitReached('budget')
    return limit


def settle(db,attempt,conservative=False):
    slot=db.execute('SELECT * FROM execution_time_slices WHERE attempt=?',(attempt,)).fetchone()
    if not slot or not slot['computing']:return
    elapsed=slot['reserved'] if conservative else min(slot['reserved'],max(0,clock(db)-slot['accounted_at']))
    db.execute('UPDATE execution_limits SET used_seconds=used_seconds+? WHERE task_id=?',(elapsed,slot['task_id']))
    db.execute('UPDATE execution_time_slices SET reserved=0,computing=0 WHERE attempt=?',(attempt,))


def reserve(db,lease):
    limit=check(db,lease.run_id)
    settle(db,lease.attempt)
    limit=check(db,lease.run_id)
    other=db.execute('SELECT COALESCE(SUM(reserved),0) AS n FROM execution_time_slices WHERE task_id=? AND computing=1',
                     (lease.run_id,)).fetchone()['n']
    remaining=limit['budget_seconds']-limit['used_seconds']-other
    if remaining<=0:raise TaskLimitReached('budget')
    now=clock(db)
    seconds=min(lease.ttl,remaining,limit['deadline_at']-now)
    db.execute('INSERT INTO execution_time_slices VALUES (?,?,?,?,1) ON CONFLICT(attempt) DO UPDATE SET accounted_at=excluded.accounted_at,reserved=excluded.reserved,computing=1',
               (lease.attempt,lease.run_id,now,seconds))
    return now+seconds


def terminate(db,task_id,reason):
    ensure(db,task_id)
    current=db.execute('SELECT status FROM execution_subjects WHERE id=?',(task_id,)).fetchone()
    cancelling=bool(current and current['status'] in ('cancelled','cancelling'))
    if cancelling:reason='cancelled'
    db.execute("UPDATE execution_limits SET termination_reason=? WHERE task_id=? AND termination_reason=''",(reason,task_id))
    message='任务总时限已到，已停止后续操作并安排资源清理。' if reason=='deadline' else '累计执行预算已用完，已停止后续操作并安排资源清理。'
    if reason=='security':message='多次请求违反任务安全策略，已停止执行并安排资源清理。'
    if cancelling:message='用户已停止任务，已提交成果保留，资源正在回收。'
    now=clock(db)
    db.execute("UPDATE runs SET status='CANCELLING',terminal_reason='deadline_or_budget',next_reconcile_at=? WHERE id=? AND status NOT IN ('SUCCEEDED','FAILED','CANCELLED')",(now,task_id))
    for table in ('agent_turns','jobs'):
        db.execute(f"UPDATE {table} SET status=?,error=?,updated=? WHERE id=? AND status IN ('queued','running','waiting','paused','cancelling')",
                   ('cancelled' if cancelling else 'failed',message,now,task_id))
    db.execute("UPDATE agent_workflows SET status=?,error=?,updated=? WHERE run_id=? AND status NOT IN ('succeeded','cancelled')",('cancelled' if cancelling else 'paused',message,now,task_id))
    db.execute("UPDATE workflow_agent_reviews SET status='cancelled' WHERE workflow IN (SELECT id FROM agent_workflows WHERE run_id=?) AND status IN ('pending','running','staged')",(task_id,))
    db.execute("UPDATE workflow_user_requests SET status='cancelled' WHERE workflow IN (SELECT id FROM agent_workflows WHERE run_id=?) AND status='pending'",(task_id,))
    request_cleanup(db,task_id)


def request_cleanup(db,task_id,attempt_id=None,external=True):
    now=clock(db)
    duration=max(1,float(os.environ.get('VIDEO_USE_CLEANUP_SECONDS','60')))
    db.execute("UPDATE execution_cleanup SET state='pending',started=COALESCE(started,?),deadline_at=COALESCE(deadline_at,?),updated=? WHERE task_id=? AND state='allocated'"+(' AND attempt=?' if attempt_id else ''),
               (now,now+duration,now,task_id)+((attempt_id,) if attempt_id else ()))
    if not external:return
    for row in db.execute("SELECT * FROM execution_provider_calls WHERE run_id=? AND status IN ('waiting','submitting')",(task_id,)).fetchall():
        db.execute('INSERT OR IGNORE INTO execution_external_cleanup(id,task_id,provider,external_id,state,error) VALUES (?,?,?,?,?,?)',
                   (row['id'],task_id,row['provider'],row['external_id'],'pending' if row['external_id'] else 'unknown',
                    '' if row['external_id'] else '提交结果未知，无法确认上游是否仍在运行。'))
    for row in db.execute("SELECT request_key,provider,external_id,state FROM workflow_provider_calls WHERE (workflow=? OR workflow IN (SELECT id FROM runs WHERE id=?)) AND state NOT IN ('succeeded','failed','cancelled','rejected')",(task_id,task_id)).fetchall():
        db.execute('INSERT OR IGNORE INTO execution_external_cleanup(id,task_id,provider,external_id,state,error) VALUES (?,?,?,?,?,?)',(row['request_key'],task_id,row['provider'],row['external_id'],'pending' if row['external_id'] else 'unknown','' if row['external_id'] else '提交结果未知，未重复调用'))
    task=db.execute('SELECT status FROM agent_turns WHERE id=?',(task_id,)).fetchone()
    if task and task['status'] in ('failed','cancelled','cancelling'):
        import json
        from .image_providers import ENDPOINTS
        for row in db.execute("SELECT s.* FROM agent_steps s JOIN agent_workflows w ON w.id=s.workflow WHERE w.run_id=? AND (s.external_id IS NOT NULL OR s.status='submitting' OR s.error_code='submission_unknown') AND s.status<>'succeeded'",(task_id,)).fetchall():
            spec=json.loads(row['spec'])
            from .operations import for_step
            op=for_step(spec)
            if not op:continue
            saved=db.execute('SELECT provider FROM workflow_provider_calls WHERE request_key=?',(row['workflow']+':'+row['id']+':'+str(row['revision']),)).fetchone()
            # Only a persisted intent can identify an external effect to clean up.
            if not saved:continue
            base=saved['provider']
            db.execute('INSERT OR IGNORE INTO execution_external_cleanup(id,task_id,provider,external_id,state,error) VALUES (?,?,?,?,?,?)',
                       (row['workflow']+':'+row['id']+':'+str(row['revision']),task_id,base,row['external_id'] or '',
                        'pending' if row['external_id'] else 'unknown','' if row['external_id'] else '提交结果未知，无法确认上游是否仍在运行。'))


def sweep(db):
    now=clock(db)
    db.execute("""UPDATE execution_external_cleanup AS cleanup SET state='pending',next_retry=0,error=''
        WHERE cleanup.state='dormant' AND cleanup.provider LIKE 'opensandbox:%'
        AND NOT EXISTS (SELECT 1 FROM execution_leases AS lease
            WHERE lease.attempt=substr(cleanup.provider,13) AND lease.active=1 AND lease.expires>?)""",(now,))
    rows=db.execute("SELECT x.task_id FROM execution_limits x JOIN (SELECT id,status FROM execution_subjects) t ON t.id=x.task_id WHERE x.deadline_at<=? AND x.termination_reason='' AND t.status IN ('queued','running','waiting','paused')",(now,)).fetchall()
    for row in rows:terminate(db,row['task_id'],'deadline')


def snapshot(store,task_id):
    with store.connect() as db:
        limit=db.execute('SELECT * FROM execution_limits WHERE task_id=?',(task_id,)).fetchone()
        if not limit:return None
        now=clock(db)
        active=db.execute('SELECT accounted_at,reserved FROM execution_time_slices WHERE task_id=? AND computing=1',(task_id,)).fetchall()
        spent=limit['used_seconds']+sum(min(r['reserved'],max(0,now-r['accounted_at'])) for r in active)
        cleanup=db.execute('SELECT c.state,c.error,c.updated FROM execution_cleanup c JOIN execution_attempts a ON a.id=c.attempt WHERE c.task_id=? ORDER BY a.generation DESC LIMIT 1',(task_id,)).fetchone()
        task=db.execute('SELECT status FROM execution_subjects WHERE id=?',(task_id,)).fetchone()
        return {'deadline_at':limit['deadline_at'],'budget_seconds':limit['budget_seconds'],
                'used_seconds':round(spent,2),'remaining_seconds':max(0,round(limit['budget_seconds']-spent,2)),
                'termination_reason':limit['termination_reason'] or ('cancelled' if task and task['status']=='cancelled' else ''),
                'cleanup':dict(cleanup) if cleanup else {'state':'not_allocated','error':''},
                'external_cleanup':[dict(r) for r in db.execute('SELECT state,error FROM execution_external_cleanup WHERE task_id=?',(task_id,)).fetchall()]}
