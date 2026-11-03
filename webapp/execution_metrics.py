"""Bounded per-attempt phase accounting; measurements never authorize results."""
from contextlib import contextmanager
from functools import wraps
import time


def initialize(db):
    db.execute('''CREATE TABLE IF NOT EXISTS execution_phase_metrics(
      attempt TEXT NOT NULL,task_id TEXT NOT NULL,step TEXT NOT NULL,phase TEXT NOT NULL,
      calls INTEGER NOT NULL,seconds REAL NOT NULL,failures INTEGER NOT NULL,
      PRIMARY KEY(attempt,phase))''')


@contextmanager
def measurement_connection(observer):
    # Atomic counters in this table have no cross-table quota or fencing
    # invariants. Use PostgreSQL row locks, not the business control lock.
    # This independent transaction must never be used for business writes.
    if observer.dsn:
        from .database import connect,PostgresConnection
        with connect(observer.dsn,observer.timeout,control_lock=False) as raw:
            yield PostgresConnection(raw)
    else:
        with observer.connect() as db:
            yield db


@contextmanager
def phase(name,actor='host',label=None,operation=''):
    from .task_leases import CURRENT
    lease=CURRENT.get();started=time.monotonic();failed=False
    from .telemetry import span, emit, CONTEXT
    span_fields={}
    try:
        from contextlib import nullcontext
        from .live_activity import span as visible_span
        labels={'prepare_environment':'准备任务环境','submit':'提交操作','remote_sync':'调用外部服务','poll':'查询外部任务','local':'执行代码','collect':'收取处理结果','validate':'检查产物'}
        visible=visible_span(actor,name,label or labels.get(name,name),operation) if name in labels else nullcontext()
        with span(name),visible:
            span_fields={key:CONTEXT.get().get(key) for key in ('span_id','parent_span_id')}
            yield
    except BaseException:failed=True;raise
    finally:
        if lease:
            from .task_lifecycle import administrative
            try:
                from .store import Store
                # Metrics use a separate short-budget connection, never the
                # business connection's 30-second control-lock timeout.
                observer=Store(lease.store.root,timeout=.05,initialize_schema=False,dsn=lease.store.dsn)
                with administrative(),measurement_connection(observer) as db:
                    db.execute('''INSERT INTO execution_phase_metrics VALUES (?,?,?,?,1,?,?)
                      ON CONFLICT(attempt,phase) DO UPDATE SET calls=execution_phase_metrics.calls+1,
                      seconds=execution_phase_metrics.seconds+excluded.seconds,failures=execution_phase_metrics.failures+excluded.failures''',
                      (lease.attempt,lease.run_id,lease.step_id or '',name,time.monotonic()-started,int(failed)))
            except Exception as exc:emit('telemetry.phase_accounting_failed','WARNING',exc=exc,phase=name,**span_fields)


def measured(name):
    def decorate(function):
        @wraps(function)
        def wrapped(*args,**kwargs):
            from .telemetry import bind
            spec=next((a for a in args[1:] if isinstance(a,dict) and isinstance(a.get('spec'),dict)),None)
            fields={}
            if spec:fields={'step_id':spec.get('id'),'revision':spec.get('revision'),'operation':spec['spec'].get('operation') or 'script'}
            if spec and isinstance(spec['spec'].get('implementation'),dict):
                fields.update(implementation=spec['spec']['implementation'].get('id'),implementation_version=spec['spec']['implementation'].get('version'))
            actor='host'
            if name=='local':actor='workflow_sandbox' if function.__name__=='local' else 'media_executor'
            operation=fields.get('operation','')
            label=('准备并执行 '+operation) if name=='local' and operation and operation!='script' else None
            with bind(**fields),phase(name,actor,label,operation):return function(*args,**kwargs)
        return wrapped
    return decorate
