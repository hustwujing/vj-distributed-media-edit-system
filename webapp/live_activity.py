"""Observable execution states, without model reasoning or authorization effects."""
from contextlib import contextmanager
import time
import uuid

ACTORS={'light_agent','sandbox_agent','workflow_sandbox','host','media_executor','external_service'}

def initialize(db):
    db.execute('''CREATE TABLE IF NOT EXISTS execution_activities(
      task_id TEXT NOT NULL,id TEXT NOT NULL,attempt TEXT NOT NULL,actor TEXT NOT NULL,
      phase TEXT NOT NULL,label TEXT NOT NULL,status TEXT NOT NULL,started REAL NOT NULL,
      updated REAL NOT NULL,operation TEXT NOT NULL DEFAULT '',step TEXT NOT NULL DEFAULT '',
      PRIMARY KEY(task_id,id))''')
    columns={r['name'] for r in db.execute('PRAGMA table_info(execution_activities)')}
    if 'sequence' not in columns:db.execute('ALTER TABLE execution_activities ADD COLUMN sequence INTEGER NOT NULL DEFAULT 0')

def record(db,task,payload):
    if payload.get('actor') not in ACTORS or payload.get('status') not in ('running','completed','failed'):return
    context=payload.get('execution') or {}
    db.execute('''INSERT INTO execution_activities(task_id,id,attempt,actor,phase,label,status,started,updated,operation,step,sequence) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)
      ON CONFLICT(task_id,id) DO UPDATE SET phase=excluded.phase,label=excluded.label,
      status=excluded.status,updated=excluded.updated,sequence=excluded.sequence
      WHERE execution_activities.status='running' AND (excluded.sequence>execution_activities.sequence
        OR (excluded.sequence=execution_activities.sequence AND (excluded.status<>'running' OR excluded.updated>=execution_activities.updated)))''',
      (task,payload['id'],context.get('attempt_id',''),payload['actor'],payload['phase'],payload['label'][:160],payload['status'],
       payload['started'],payload.get('updated',time.time()),payload.get('operation','')[:120],context.get('step_id') or '',payload.get('sequence',1 if payload['status']=='running' else 2)))
    return {key:payload.get(key,'') for key in ('id','actor','phase','status','operation')}

def current(store,task):
    rows=store.query("SELECT c.*,a.status AS attempt_status,a.generation AS generation FROM execution_activities c LEFT JOIN execution_attempts a ON a.id=c.attempt WHERE c.task_id=? ORDER BY CASE WHEN c.status='running' AND (a.status='running' OR a.status IS NULL) THEN 0 ELSE 1 END,c.updated DESC LIMIT 1000",(task,))
    for row in rows:
        if row['status']=='running' and row['attempt_status'] not in (None,'running'):row['status']='interrupted'
    return rows

class Activity:
    def __init__(self,actor,phase,label,operation=''):
        from .task_leases import CURRENT
        self.lease=CURRENT.get();self.id=uuid.uuid4().hex;self.actor=actor;self.phase=None;self.label=label;self.operation=operation;self.started=time.time();self.sequence=0
        self.update(phase,label)
    def update(self,phase,label=None,status='running'):
        if not self.lease:return
        if self.phase==phase and status=='running':return
        self.phase=phase;self.label=label or self.label;self.sequence+=1
        try:
            import json
            from .store import Store
            from .task_lifecycle import administrative
            from .execution_metrics import measurement_connection
            payload={'id':self.id,'sequence':self.sequence,'actor':self.actor,'phase':phase,'label':self.label,'status':status,
                     'started':self.started,'updated':time.time(),'operation':self.operation,
                     'execution':{'attempt_id':self.lease.attempt,'generation':self.lease.generation,
                                  'worker':self.lease.worker,'step_id':self.lease.step_id}}
            observer=Store(self.lease.store.root,timeout=.05,initialize_schema=False,dsn=self.lease.store.dsn)
            # This observation never changes business state and must not wait on
            # the scheduler's global control lock or an expired business fence.
            with administrative(),measurement_connection(observer) as db:
                db.execute('INSERT INTO agent_events(turn_id,kind,payload,created) VALUES (?,?,?,?)',
                           (self.lease.turn_id,'work',json.dumps(payload,ensure_ascii=False),time.time()))
                record(db,self.lease.turn_id,payload)
            from .agent_stream import bus
            bus().publish_async(self.lease.turn_id,'work',payload)
            from .telemetry import emit
            emit('execution.activity',task_id=self.lease.turn_id,activity_id=self.id,actor=self.actor,phase=phase,operation=self.operation,outcome=status)
        except Exception as exc:
            from .telemetry import emit
            emit('activity.record_failed','WARNING',exception_type=type(exc).__name__)
    def model_phase(self,phase):
        labels={'analysis':'模型分析中','responding':'生成回复','prepare_tool':'准备工具调用','writing':'生成文件修改'}
        self.update(phase,labels[phase])

@contextmanager
def span(actor,phase,label,operation=''):
    activity=Activity(actor,phase,label,operation)
    try:yield activity
    except BaseException:
        activity.update(activity.phase or phase,status='failed');raise
    else:activity.update(activity.phase or phase,status='completed')


def observed(actor,phase,label,operation=''):
    from functools import wraps
    def decorate(function):
        @wraps(function)
        def run(*args,**kwargs):
            with span(actor,phase,label,operation):return function(*args,**kwargs)
        return run
    return decorate
