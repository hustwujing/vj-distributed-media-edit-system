"""Durable execution ownership. SQLite today; no process-local scheduling locks.

All time decisions use the database clock. Binding a Lease fences every Store
transaction, including callbacks on gateway threads, against stale generations.
"""
from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
import os
import json
import threading
import time
import uuid

CURRENT = ContextVar('video_use_execution', default=None)
CLOCK = "((julianday('now') - 2440587.5) * 86400.0)"


class LeaseLost(RuntimeError):
    pass


class TaskQuotaReached(ValueError):
    def __init__(self,message,*,category=None,scope=None,used=None,limit=None,reset_at=None):
        super().__init__(message)
        self.diagnostic={'code':(scope+'_quota_exhausted') if scope else 'quota_exhausted',
                         'category':category,'scope':scope,'used':used,'limit':limit}
        if reset_at is not None:self.diagnostic['reset_at']=reset_at


def model_call_limit():
    from .agent_profiles import current
    return current().budget['task_models']


def quota_status(store,task_id,category,limit):
    """Read authoritative counters; never infer budget state from error prose."""
    rows=store.query('SELECT used FROM execution_budgets WHERE turn_id=? AND category=?',(task_id,category))
    used=rows[0]['used'] if rows else 0
    if used>=limit:return {'category':category,'scope':'task','used':used,'limit':limit,'blocked':True}
    owners=store.query('SELECT p.owner FROM projects p JOIN (SELECT id,project FROM agent_turns UNION ALL SELECT id,project FROM jobs) t ON t.project=p.id WHERE t.id=?',(task_id,))
    daily=daily_quota(store,owners[0]['owner'],category) if owners else None
    return daily if daily else {'category':category,'scope':'task','used':used,'limit':limit,'blocked':False}


def daily_quota(store,owner,category):
    day=int(store.now()//86400)
    cap=int(os.environ.get('VIDEO_USE_USER_'+category.upper()+'_DAILY','200' if category=='model' else '40'))
    rows=store.query('SELECT used FROM execution_daily_usage WHERE owner=? AND day=? AND category=?',(owner,day,category))
    used=rows[0]['used'] if rows else 0
    return {'category':category,'scope':'daily','used':used,'limit':cap,'reset_at':(day+1)*86400,'blocked':True} if used>=cap else None


def quota_error(state):
    from datetime import datetime,timezone
    scope=state['scope']
    label={'model':'模型','planning_model':'规划模型','image':'图片','local_image':'本地图片处理','video':'视频','transcribe':'转写'}.get(state['category'],'操作')
    prefix={'task':'本任务','daily':'今日','turn':'本轮'}.get(scope,'当前')
    reset=state.get('reset_at')
    advice=('额度将于 '+datetime.fromtimestamp(reset,timezone.utc).strftime('%Y-%m-%d %H:%M UTC')+' 恢复，恢复前重复提交无效。') if scope=='daily' and reset else '请等待每日额度恢复或联系管理员。' if scope=='daily' else '已完成成果保留，请确认新的收尾方案。' if scope=='task' else '可在新一轮中继续检查。'
    return TaskQuotaReached(f"{prefix}{label}调用额度已用完（{state['used']}/{state['limit']}）。{advice}",
                            **{k:state.get(k) for k in ('category','scope','used','limit','reset_at')})


def record_quota(store,task,exc):
    from .agent_store import event
    try:event(store,task,'quota',{**exc.diagnostic,'message':str(exc)})
    except Exception as error:
        # A receipt write must not suppress the original quota response.
        from .telemetry import emit
        emit('quota.record_failed','WARNING',exc=error)


def blocked_quota(store,task):
    rows=store.query("SELECT payload FROM agent_events WHERE turn_id=? AND kind='quota' ORDER BY seq DESC LIMIT 1",(task,))
    if not rows:return None
    block=json.loads(rows[0]['payload'])
    if block.get('scope')=='daily':
        owners=store.query('SELECT p.owner FROM projects p JOIN agent_turns t ON t.project=p.id WHERE t.id=?',(task,))
        state=daily_quota(store,owners[0]['owner'],block['category']) if owners else None
    else:state=quota_status(store,task,block['category'],block['limit'])
    return {**state,'message':str(quota_error(state))} if state and state['blocked'] else None


def now(db):
    return db.execute('SELECT '+CLOCK+' AS value').fetchone()['value']


def initialize(store):
    with store.connect() as db:
        db.executescript('''
        CREATE TABLE IF NOT EXISTS execution_workers (
          id TEXT PRIMARY KEY, node TEXT NOT NULL, release TEXT NOT NULL,
          model TEXT NOT NULL, heartbeat REAL NOT NULL, draining INTEGER NOT NULL DEFAULT 0
        );
        CREATE TABLE IF NOT EXISTS execution_leases (
          turn_id TEXT NOT NULL, worker TEXT NOT NULL, attempt TEXT NOT NULL UNIQUE,
          generation INTEGER NOT NULL, kind TEXT NOT NULL, expires REAL NOT NULL,
          active INTEGER NOT NULL, interrupted INTEGER NOT NULL DEFAULT 0,
          release TEXT NOT NULL, slot TEXT PRIMARY KEY
        );
        CREATE TABLE IF NOT EXISTS execution_attempts (
          id TEXT PRIMARY KEY, turn_id TEXT NOT NULL, worker TEXT NOT NULL,
          generation INTEGER NOT NULL, kind TEXT NOT NULL, started REAL NOT NULL,
          finished REAL, status TEXT NOT NULL, runtime TEXT NOT NULL DEFAULT '',
          uid INTEGER, checkpoint TEXT NOT NULL DEFAULT ''
        );
        CREATE INDEX IF NOT EXISTS execution_expiration ON execution_leases(active,expires);
        CREATE TABLE IF NOT EXISTS execution_checkpoints (
          project TEXT PRIMARY KEY, attempt TEXT NOT NULL, path TEXT NOT NULL,
          manifest TEXT NOT NULL, updated REAL NOT NULL
        );
        CREATE TABLE IF NOT EXISTS execution_budgets (
          turn_id TEXT NOT NULL, category TEXT NOT NULL, used INTEGER NOT NULL,
          PRIMARY KEY(turn_id,category)
        );
        CREATE TABLE IF NOT EXISTS execution_provider_calls (
          id TEXT PRIMARY KEY, turn_id TEXT NOT NULL, attempt TEXT NOT NULL,
          status TEXT NOT NULL, external_id TEXT NOT NULL, provider TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS execution_job_results (
          job_id TEXT PRIMARY KEY, attempt TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS execution_daily_usage (
          owner TEXT NOT NULL, day INTEGER NOT NULL, category TEXT NOT NULL,
          used INTEGER NOT NULL, PRIMARY KEY(owner,day,category)
        );
        ''')
        if 'exit_reason' not in {r['name'] for r in db.execute('PRAGMA table_info(execution_attempts)')}:
            db.execute("ALTER TABLE execution_attempts ADD COLUMN exit_reason TEXT NOT NULL DEFAULT ''")
        if 'step_id' not in {r['name'] for r in db.execute('PRAGMA table_info(execution_attempts)')}:
            db.execute("ALTER TABLE execution_attempts ADD COLUMN step_id TEXT NOT NULL DEFAULT ''")
        if 'permission_profile' not in {r['name'] for r in db.execute('PRAGMA table_info(execution_attempts)')}:
            db.execute("ALTER TABLE execution_attempts ADD COLUMN permission_profile TEXT NOT NULL DEFAULT ''")
        if 'bytes' not in {r['name'] for r in db.execute('PRAGMA table_info(execution_job_results)')}:
            db.execute('ALTER TABLE execution_job_results ADD COLUMN bytes INTEGER NOT NULL DEFAULT 0')
        from . import task_lifecycle
        task_lifecycle.initialize(db)
        from .task_usage import initialize as initialize_usage
        initialize_usage(db)
        from .checkpoints import initialize as initialize_checkpoints
        initialize_checkpoints(db)
        from .scheduling import initialize as initialize_resources
        initialize_resources(db)
    from .observation_schema import initialize as initialize_observation
    initialize_observation(store)


@dataclass
class Lease:
    store: object
    turn_id: str
    worker: str
    attempt: str
    generation: int
    kind: str
    recovered: bool
    prior_kind: str
    ttl: float
    lost: threading.Event = field(default_factory=threading.Event)
    runtime: object = None
    runtime_factory: object = None
    step_id: str | None = None
    control_only: bool = False

    def validate(self, db):
        if self.runtime and (self.runtime.path/'revoked').exists():
            self.lost.set()
        row = db.execute('SELECT * FROM execution_leases WHERE attempt=?', (self.attempt,)).fetchone()
        if (self.lost.is_set() or not row or not row['active'] or row['worker'] != self.worker
                or row['attempt'] != self.attempt or row['generation'] != self.generation
                or row['expires'] <= now(db)):
            self.lost.set()
            raise LeaseLost('Execution ownership expired or was replaced')
        cleanup=db.execute('SELECT state FROM execution_cleanup WHERE attempt=?',(self.attempt,)).fetchone()
        if cleanup and cleanup['state']!='allocated':raise LeaseLost('Task resource cleanup has started')
        from .task_lifecycle import check
        check(db,self.turn_id,self.attempt)
        if not self.control_only:
            table='jobs' if self.kind=='manual' else 'agent_turns'
            task=db.execute(f'SELECT status FROM {table} WHERE id=?',(self.turn_id,)).fetchone()
            if not task or task['status'] in ('cancelling','cancelled','failed'):
                self.lost.set()
                raise LeaseLost('Task no longer authorizes business execution')

    @contextmanager
    def bind(self):
        token = CURRENT.set(self)
        try:
            yield self
        finally:
            CURRENT.reset(token)

    def check(self):
        with self.bind(), self.store.connect() as db:
            self.validate(db)

    def renew(self):
        with self.bind(), self.store.connect() as db:
            self.validate(db)
            from . import task_lifecycle
            limit=task_lifecycle.check(db,self.turn_id)
            slot=db.execute('SELECT computing FROM execution_time_slices WHERE attempt=?',(self.attempt,)).fetchone()
            expires=task_lifecycle.reserve(db,self) if slot and slot['computing'] else min(now(db)+self.ttl,limit['deadline_at'])
            db.execute('UPDATE execution_leases SET expires=? WHERE attempt=?',
                       (expires, self.attempt))
        if self.runtime:self.runtime.pulse(max(0,expires-now_value(self.store)))

    def start_compute(self):
        from .task_lifecycle import reserve
        with self.bind(),self.store.connect() as db:
            expires=reserve(db,self)
            db.execute('UPDATE execution_leases SET expires=MIN(expires,?) WHERE attempt=?',(expires,self.attempt))

    def stop_compute(self,conservative=False):
        from .task_lifecycle import administrative,settle
        with administrative(),self.store.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            settle(db,self.attempt,conservative)

    def consume(self, category, limit):
        with self.bind(), self.store.connect() as db:
            row = db.execute('SELECT used FROM execution_budgets WHERE turn_id=? AND category=?',
                             (self.turn_id, category)).fetchone()
            used = row['used'] if row else 0
            if used >= limit:
                raise quota_error({'category':category,'scope':'task','used':used,'limit':limit})
            owner=db.execute('SELECT p.owner FROM projects p JOIN (SELECT id,project FROM agent_turns UNION ALL SELECT id,project FROM jobs) t ON t.project=p.id WHERE t.id=?',(self.turn_id,)).fetchone()['owner']
            day=int(now(db)//86400)
            daily=db.execute('SELECT used FROM execution_daily_usage WHERE owner=? AND day=? AND category=?',(owner,day,category)).fetchone()
            consumed=daily['used'] if daily else 0
            cap=int(os.environ.get('VIDEO_USE_USER_'+category.upper()+'_DAILY', '200' if category=='model' else '40'))
            if consumed>=cap:raise quota_error({'category':category,'scope':'daily','used':consumed,'limit':cap,'reset_at':(day+1)*86400})
            db.execute('INSERT INTO execution_budgets VALUES (?,?,?) ON CONFLICT(turn_id,category) DO UPDATE SET used=excluded.used',
                       (self.turn_id, category, used + 1))
            db.execute('INSERT INTO execution_daily_usage VALUES (?,?,?,?) ON CONFLICT(owner,day,category) DO UPDATE SET used=excluded.used',
                       (owner,day,category,consumed+1))

    def release(self, interrupted=False):
        from .task_lifecycle import administrative,settle
        with administrative(), self.store.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            row=db.execute('SELECT * FROM execution_leases WHERE attempt=?',(self.attempt,)).fetchone()
            if not row or row['attempt']!=self.attempt or row['worker']!=self.worker:
                raise LeaseLost('Execution was replaced')
            settle(db,self.attempt,interrupted)
            task=db.execute('SELECT status FROM agent_turns WHERE id=? UNION ALL SELECT status FROM jobs WHERE id=?',(self.turn_id,self.turn_id)).fetchone()
            limits=db.execute('SELECT termination_reason FROM execution_limits WHERE task_id=?',(self.turn_id,)).fetchone()
            reason=(limits['termination_reason'] if limits else '') or ('resource_wait' if getattr(self,'waiting_for_resource',False) else 'interrupted' if interrupted else task['status'] if task and task['status'] in ('failed','cancelled') else '')
            db.execute('UPDATE execution_attempts SET exit_reason=? WHERE id=?',(reason,self.attempt))
            db.execute('UPDATE execution_attempts SET status=?,finished=? WHERE id=?',
                       ('interrupted' if interrupted else 'resource_wait' if getattr(self,'waiting_for_resource',False) else 'finished', now(db), self.attempt))
            db.execute('UPDATE execution_leases SET active=0,interrupted=? WHERE attempt=?',
                       (int(interrupted), self.attempt))


class Coordinator:
    def __init__(self, store, node, release, model='', ttl=45):
        if ttl < 3:
            raise ValueError('Lease duration must be at least 3 seconds')
        self.store, self.node, self.release, self.model, self.ttl = store, node, release, model, ttl
        self.worker = uuid.uuid4().hex
        initialize(store)
        from .operations import catalog
        self.operation_catalog=json.dumps(catalog(),ensure_ascii=False)
        self.heartbeat()

    def heartbeat(self, draining=False):
        with self.store.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            from .task_lifecycle import sweep
            sweep(db)
            db.execute('INSERT INTO execution_workers VALUES (?,?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET heartbeat=excluded.heartbeat,draining=MAX(execution_workers.draining,excluded.draining)',
                       (self.worker, self.node, self.release, self.model, now(db), int(draining)))
            db.execute('INSERT OR IGNORE INTO execution_operation_catalog VALUES (?,?,?)',(self.worker,self.operation_catalog,now(db)))
            from .scheduling import limits
            db.execute('INSERT INTO execution_scheduler_configs VALUES (?,?,?) ON CONFLICT(worker) DO UPDATE SET settings=excluded.settings,updated=excluded.updated',(self.worker,json.dumps(limits()),now(db)))
            expired=db.execute('SELECT l.attempt,l.turn_id FROM execution_leases l JOIN (SELECT id,status FROM agent_turns UNION ALL SELECT id,status FROM jobs) t ON t.id=l.turn_id WHERE l.active=1 AND l.expires<=? AND t.status IN (\'cancelled\',\'failed\',\'succeeded\')',(now(db),)).fetchall()
            for row in expired:
                db.execute('UPDATE execution_leases SET active=0 WHERE attempt=?',(row['attempt'],))
                db.execute("UPDATE execution_attempts SET status='expired',finished=? WHERE id=?",(now(db),row['attempt']))

    def claim(self, turn_id, kind, per_user=2, step_id=None):
        with self.store.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            timestamp = now(db)
            own=db.execute('SELECT draining FROM execution_workers WHERE id=?',(self.worker,)).fetchone()
            if own and own['draining']:return None
            table = 'jobs' if kind == 'manual' else 'agent_turns'
            turn = db.execute(f'SELECT t.*,p.owner FROM {table} t JOIN projects p ON p.id=t.project WHERE t.id=?', (turn_id,)).fetchone()
            waiting=db.execute('SELECT next_retry FROM execution_waits WHERE task_id=?',(turn_id,)).fetchone()
            if waiting and waiting['next_retry']>timestamp and turn and turn['status']!='cancelling':return None
            if not turn or turn['status'] in ('succeeded', 'failed', 'cancelled', 'paused'):
                return None
            if kind=='agent' and turn['status']!='cancelling':
                from .harness_switching import pending
                if pending(db,turn_id):return None
            pausing=db.execute('''SELECT c.turn_id FROM workflow_control c JOIN agent_workflows w ON w.turn_id=c.turn_id
                WHERE c.turn_id=? AND c.pause_requested=1 AND w.status IN ('ready','reviewing','resuming')''',(turn_id,)).fetchone()
            if pausing and turn['status']!='cancelling':return None
            if turn['status']=='cancelling' and step_id is not None:return None
            from . import task_lifecycle
            limit=task_lifecycle.ensure(db,turn_id,turn['created'])
            if timestamp>=limit['deadline_at'] or limit['termination_reason']:
                task_lifecycle.terminate(db,turn_id,limit['termination_reason'] or 'deadline')
                return None
            environment_binding=None
            if kind=='workflow' and turn['status']!='cancelling':
                from .scheduling import available_steps
                selected=available_steps(self.store,turn_id,timestamp)
                if step_id is not None:
                    if step_id not in selected:return None
                elif selected:step_id=selected[0]
                if step_id:
                    from .operations import for_step
                    from .operation_lifecycle import for_operation,Lifecycle
                    from .runtime_image import fingerprint
                    detail=db.execute('SELECT s.workflow,s.spec,s.bindings FROM agent_steps s JOIN agent_workflows w ON w.id=s.workflow WHERE w.turn_id=? AND s.id=?',(turn_id,step_id)).fetchone()
                    try:operation=for_step(json.loads(detail['spec']))
                    except ValueError:return None
                    if operation and not operation.node_enabled():return None
                    if operation is None or for_operation(operation).lifecycle==Lifecycle.LOCAL:
                        bindings=json.loads(detail['bindings']);environment=fingerprint()
                        if bindings.get('_environment') and bindings['_environment']!=environment:return None
                        if not bindings.get('_environment'):
                            environment_binding=(json.dumps({**bindings,'_environment':environment}),detail['workflow'],step_id)
            slot=turn_id+'|'+step_id if step_id else turn_id
            # Root Agent/review/control execution excludes all sibling steps;
            # independent step slots may coexist for the same workflow.
            active=db.execute('SELECT slot FROM execution_leases WHERE turn_id=? AND active=1 AND expires>?',(turn_id,timestamp)).fetchall()
            if turn['status']!='cancelling' and any(r['slot']==turn_id or not step_id for r in active):return None
            prior = db.execute('SELECT * FROM execution_leases WHERE slot=?', (slot,)).fetchone()
            if prior and prior['active'] and prior['expires'] > timestamp:
                return None
            if prior:task_lifecycle.settle(db,prior['attempt'],True)
            limit=task_lifecycle.ensure(db,turn_id)
            if limit['used_seconds']>=limit['budget_seconds']:
                task_lifecycle.terminate(db,turn_id,'budget')
                return None
            control_only=turn['status']=='cancelling'
            node_count=db.execute('''SELECT COUNT(*) AS n FROM execution_attempts a JOIN execution_workers w ON w.id=a.worker
                WHERE w.node=? AND (EXISTS(SELECT 1 FROM execution_cleanup c WHERE c.attempt=a.id AND c.state<>'complete')
                OR EXISTS(SELECT 1 FROM execution_leases l WHERE l.attempt=a.id AND l.active=1 AND l.expires>?))''',
                                  (self.node,timestamp)).fetchone()['n']
            if not control_only and node_count>=int(os.environ.get('VIDEO_USE_NODE_CONCURRENCY',os.environ.get('VIDEO_USE_CONCURRENCY','2'))):
                db.execute("INSERT INTO execution_waits VALUES (?,'node_limit',0,?) ON CONFLICT(task_id) DO UPDATE SET reason='node_limit',updated=excluded.updated",(turn_id,timestamp))
                return None
            count = db.execute('SELECT COUNT(*) AS n FROM execution_leases l JOIN (SELECT id,project FROM agent_turns UNION ALL SELECT id,project FROM jobs) t ON t.id=l.turn_id JOIN projects p ON p.id=t.project WHERE l.active=1 AND l.expires>? AND p.owner=?',
                               (timestamp, turn['owner'])).fetchone()['n']
            if not control_only and count >= per_user:
                db.execute("INSERT INTO execution_waits VALUES (?,'user_limit',0,?) ON CONFLICT(task_id) DO UPDATE SET reason='user_limit',updated=excluded.updated",(turn_id,timestamp))
                return None
            if kind in ('agent','manual') and turn['status'] not in ('queued', 'running', 'cancelling'):
                return None
            # Mutual exclusion spans Agent and manual jobs, including the window
            # where an API has marked a task cancelled but cleanup is still live.
            busy = db.execute('SELECT l.turn_id FROM execution_leases l JOIN (SELECT id,project FROM agent_turns UNION ALL SELECT id,project FROM jobs) t ON t.id=l.turn_id WHERE t.project=? AND l.active=1 AND l.expires>? AND l.turn_id<>?',
                              (turn['project'],timestamp,turn_id)).fetchone()
            cleaning=db.execute("""SELECT c.task_id FROM execution_cleanup c
              JOIN (SELECT id,project FROM agent_turns UNION ALL SELECT id,project FROM jobs) t ON t.id=c.task_id
              JOIN execution_attempts a ON a.id=c.attempt JOIN execution_workers ew ON ew.id=a.worker
              WHERE t.project=? AND c.task_id<>? AND c.state<>'complete'
              AND (c.node=? OR ew.heartbeat>?) LIMIT 1""",
              (turn['project'],turn_id,self.node,timestamp-float(os.environ.get('VIDEO_USE_NODE_OFFLINE_SECONDS','60')))).fetchone()
            if not control_only and cleaning:
                db.execute("INSERT INTO execution_waits VALUES (?,'cleanup',0,?) ON CONFLICT(task_id) DO UPDATE SET reason='cleanup',updated=excluded.updated",(turn_id,timestamp))
                return None
            if not control_only and busy:
                db.execute("INSERT INTO execution_waits VALUES (?,'project_busy',0,?) ON CONFLICT(task_id) DO UPDATE SET reason='project_busy',updated=excluded.updated",(turn_id,timestamp))
                return None
            if kind == 'workflow':
                w = db.execute('SELECT status FROM agent_workflows WHERE turn_id=?', (turn_id,)).fetchone()
                if not w or w['status'] not in ('ready', 'staged', 'cancelling', 'resuming'):
                    return None
            recovered = bool(prior and (prior['active'] or prior['interrupted'])) or bool(not prior and turn['status']=='running')
            generation = db.execute('SELECT COALESCE(MAX(generation),0)+1 AS next FROM execution_attempts WHERE turn_id=?',(turn_id,)).fetchone()['next']
            from .scheduling import admit
            resources={'cpu':0,'memory_mb':0,'provider':''} if control_only else admit(db,self.store,turn_id,kind,self.node,timestamp,step_id=step_id)
            if resources is None:
                db.execute("INSERT INTO execution_waits VALUES (?,'resource',0,?) ON CONFLICT(task_id) DO UPDATE SET reason='resource',updated=excluded.updated",(turn_id,timestamp))
                return None
            if environment_binding:db.execute('UPDATE agent_steps SET bindings=? WHERE workflow=? AND id=?',environment_binding)
            attempt = uuid.uuid4().hex
            db.execute('INSERT INTO execution_resource_claims VALUES (?,?,?,?,?,?,?)',(attempt,turn_id,self.node,resources['cpu'],resources['memory_mb'],resources['provider'],timestamp))
            db.execute('INSERT INTO execution_leases VALUES (?,?,?,?,?,?,1,0,?,?) ON CONFLICT(slot) DO UPDATE SET worker=excluded.worker,attempt=excluded.attempt,generation=excluded.generation,kind=excluded.kind,expires=excluded.expires,active=1,interrupted=0,release=excluded.release',
                       (turn_id, self.worker, attempt, generation, kind, min(timestamp + self.ttl,limit['deadline_at']), self.release,slot))
            if prior and prior['active']:
                db.execute("UPDATE execution_attempts SET status='expired',finished=? WHERE id=?", (timestamp, prior['attempt']))
            db.execute('INSERT INTO execution_attempts(id,turn_id,worker,generation,kind,started,status,step_id) VALUES (?,?,?,?,?,?,?,?)',
                       (attempt, turn_id, self.worker, generation, kind, timestamp, 'running',resources.get('step_id') or ''))
            db.execute('DELETE FROM execution_waits WHERE task_id=?',(turn_id,))
            return Lease(self.store, turn_id, self.worker, attempt, generation, kind, recovered,
                         prior['kind'] if prior else 'agent', self.ttl,step_id=resources.get('step_id'),control_only=control_only)

    def candidates(self):
        rows=self.store.query("""SELECT t.id,t.status,w.status AS workflow_status,w.id AS workflow
          FROM agent_turns t LEFT JOIN agent_workflows w ON w.turn_id=t.id
          WHERE t.status IN ('queued','running','waiting','cancelling') ORDER BY t.created LIMIT 100""")
        timestamp=self.store.now();ready=[]
        from .scheduling import available_steps
        for row in rows:
            leases=self.store.query('SELECT * FROM execution_leases WHERE turn_id=?',(row['id'],))
            active=[lease for lease in leases if lease['active'] and lease['expires']>timestamp]
            if row['status']=='cancelling':
                if not any(lease['slot']==row['id'] for lease in active):ready.append(row)
                continue
            if row['workflow_status'] in ('ready','reviewing','resuming'):
                control=self.store.query('SELECT pause_requested FROM workflow_control WHERE turn_id=?',(row['id'],))
                if control and control[0]['pause_requested']:continue
            if row['workflow_status']=='ready':
                if not active:
                    from .workflow_review import reconcile
                    if reconcile(self.store,row['id']):
                        fresh=self.store.query('SELECT t.id,t.status,w.status AS workflow_status,w.id AS workflow FROM agent_turns t JOIN agent_workflows w ON w.turn_id=t.id WHERE t.id=?',(row['id'],))[0]
                        if fresh['status'] in ('queued','running','waiting'):ready.append(fresh)
                        continue
                steps=available_steps(self.store,row['id'],timestamp)
                ready.extend(dict(row,step_id=step) for step in steps)
                if not steps and not active:
                    unfinished=self.store.query("SELECT id FROM agent_steps WHERE workflow=? AND status<>'succeeded'",(row['workflow'],))
                    if not unfinished:ready.append(row)
            elif not active:ready.append(row)
        return ready

    def manual_candidates(self):
        return self.store.query('''SELECT j.id,j.status FROM jobs j
          LEFT JOIN execution_leases l ON l.turn_id=j.id
          WHERE j.status IN ('queued','running')
          AND NOT EXISTS(SELECT 1 FROM manual_operation_runs m WHERE m.job_id=j.id AND m.next_poll>'''+CLOCK+''')
          AND (l.turn_id IS NULL OR l.active=0 OR l.expires<='''+CLOCK+''')
          ORDER BY j.created LIMIT 100''')


def now_value(store):
    with store.connect() as db:return now(db)
