"""Dynamic graph integration with the existing resource scheduler and leases."""
import json
from . import workflow_graph as graph
from .workflow_state_axes import is_external_reconcile_task


def paused_task_allows_dispatch(task,paused_business,barrier,repair_trigger=False):
    # During an input barrier, can_dispatch/the drain-set SQL owns admission.
    # A sibling's pause must not stop already-started work from draining.
    return (barrier is not None or not paused_business or task.get('control_kind')
            or task['purpose'] in ('repair','evidence') or task.get('repair_count',0)>0
            or task.get('user_revision_count',0)>0 or repair_trigger)


def external_reconcile(run,task):
    return run['status']=='WAITING_USER' and task['status']=='ready' and is_external_reconcile_task(task)


def candidates(store):
    now=store.now()
    rows=store.query('''
        SELECT w.id AS id,t.status AS status,w.status AS workflow_status,w.id AS workflow,s.id AS step_id,
          s.control_kind,s.purpose,s.phase,s.external_id,s.repair_count,s.user_revision_count,w.replan_after_json,w.repair_context_json,w.input_barrier_sequence,
          EXISTS(SELECT 1 FROM run_tasks held WHERE held.run_id=w.id AND held.selected=1 AND held.status='paused') AS paused_business
        FROM runs w JOIN run_work_items s ON s.run_id=w.id JOIN execution_subjects t ON t.id=w.id
        WHERE (w.status='ACTIVE' OR (s.control_kind='MESSAGE' AND w.status IN ('WAITING_USER','SUCCEEDED'))
          OR (w.status='WAITING_USER' AND s.phase IN ('poll','collect') AND s.external_id<>'')
          OR (w.status='WAITING_USER' AND w.input_barrier_sequence IS NOT NULL AND w.terminal_reason NOT IN ('user_paused','user_rejected')))
          AND (w.scope_confirmed=1 OR s.control_kind<>'')
          AND s.selected=1 AND s.status='ready' AND s.lease_id='' AND s.next_run_at<=?
          AND (w.input_barrier_sequence IS NULL OR s.control_kind<>'' OR EXISTS(SELECT 1 FROM run_input_drain d WHERE d.run_id=w.id AND d.task_id=s.id AND d.revision=s.revision))
          AND (t.status NOT IN ('succeeded','failed','cancelled','cancelling') OR (t.status='succeeded' AND s.control_kind='MESSAGE'))
          AND NOT EXISTS(SELECT 1 FROM execution_leases l WHERE l.slot=w.id||'|'||s.id AND l.active=1 AND l.expires>?)
        ORDER BY w.created_at,s.next_run_at,s.created_at LIMIT 100''',(now,now))
    result=[]
    for row in rows:
        item=dict(row);paused=bool(item.pop('paused_business'))
        context=json.loads(item.pop('repair_context_json'));triggers=json.loads(item.pop('replan_after_json'))
        repair_trigger=bool(context.get('active') and item['step_id'] in triggers)
        barrier=item.pop('input_barrier_sequence')
        reconciling=item['workflow_status']=='WAITING_USER' and item['phase'] in ('poll','collect') and bool(item['external_id'])
        if not reconciling and not paused_task_allows_dispatch(item,paused,barrier,repair_trigger):continue
        item.pop('phase');item.pop('external_id')
        for key in ('control_kind','purpose','repair_count','user_revision_count'):item.pop(key)
        result.append(dict(item,dynamic=True))
    return result


def available(store,run,now):
    messages_only=run['status']=='SUCCEEDED' or run['status']=='WAITING_USER' and (run['input_barrier_sequence'] is None or run['terminal_reason'] in ('user_paused','user_rejected'))
    if run['status'] not in ('ACTIVE','WAITING_USER','SUCCEEDED'):return []
    leased={r['slot'] for r in store.query('SELECT slot FROM execution_leases WHERE run_id=? AND active=1 AND expires>?',(run['id'],now))}
    if run['id'] in leased:return []
    done={task['id'] for task in graph.work_items(run) if task['status']=='succeeded'}
    paused_business=any(t['status']=='paused' and not t.get('control_kind') for t in graph.work_items(run))
    repair_context=run.get('repair_context',{})
    from .run_inputs import can_dispatch
    return [task['id'] for task in graph.work_items(run) if (run['scope_confirmed'] or task.get('control_kind')) and can_dispatch(run,task) and task['status']=='ready' and not task['lease_id'] and task['next_run_at']<=now
            and (not messages_only or task.get('control_kind')=='MESSAGE' or external_reconcile(run,task))
            and (external_reconcile(run,task) or paused_task_allows_dispatch(task,paused_business,run['input_barrier_sequence'],
                 bool(repair_context.get('active') and task['id'] in run['replan_after'])))
            and run['id']+'|'+task['id'] not in leased
            and all(edge['upstream_task_id'] in done for edge in run['dependencies'] if edge['downstream_task_id']==task['id'])]


def progressing(store,run,item,now=None,dispatchable=None):
    """Whether this item is in flight or can advance without a graph change."""
    now=store.now() if now is None else now
    if item['status'] in ('leased','running','waiting','reviewing'):return True
    dispatchable=set(available(store,run,now)) if dispatchable is None else dispatchable
    if item['id'] in dispatchable:return True
    if item['status']!='retry_wait':return False
    from .run_inputs import can_dispatch
    parents={edge['upstream_task_id'] for edge in run['dependencies'] if edge['downstream_task_id']==item['id']}
    done={task['id'] for task in graph.work_items(run) if task['status']=='succeeded'}
    return can_dispatch(run,item) and parents<=done


def requirements(store,run,step_id):
    task=next((t for t in graph.work_items(run) if t['id']==step_id),None)
    if not task:raise ValueError('Unknown dynamic task')
    result={'cpu':1,'memory_mb':512,'gpu':False,'provider':'','step_id':step_id}
    if task.get('control_kind'):return result
    if task['type']=='agent':
        spec=graph.Task.model_validate(task['spec'])
        from .agent_runtime_capabilities import validate as validate_runtime
        runtime=('bwrap_gpu' if spec.runtime_requirements.requires_gpu else
                 'opensandbox_cpu' if task['profile'].get('harness')=='native-implementer-v1' else 'bwrap_cpu')
        validate_runtime(spec.runtime_requirements.model_dump(),selected=runtime)
        # A native Harness includes the gVisor sentry, model CLI and media
        # working set.  Its resource envelope is materially larger than a
        # fixed command sandbox; 2 GiB lets ordinary frame processing OOM the
        # sentry and makes the control WebSocket disappear mid-turn.
        if runtime=='opensandbox_cpu':result.update(cpu=4,memory_mb=8192)
        else:result.update(cpu=2,memory_mb=4096 if runtime=='bwrap_gpu' else 2048)
        result.update(gpu=spec.runtime_requirements.requires_gpu,runtime_target=runtime)
        return result
    spec=graph.Task.model_validate(task['spec'])
    if spec.type=='script':result.update(cpu=2,memory_mb=2048)
    else:
        from .operations import get
        op=get(spec.operation,spec.implementation)
        result.update(cpu=op.cpu,memory_mb=op.memory_mb,gpu=op.gpu,provider=op.concurrency_group)
        if task['phase']=='poll':result.update(cpu=1,memory_mb=256,gpu=False)
    return result


def bind_claim(store,db,run,step_id,attempt,generation):
    task=next(t for t in graph.work_items(run) if t['id']==step_id)
    from .run_inputs import can_dispatch
    if not can_dispatch(graph.get(store,run['id']),task):raise ValueError('Input barrier prevents this claim')
    if not db.execute(f'''UPDATE {graph.work_table(task)} SET lease_id=?,lease_generation=?,attempts=attempts+1,
        wait_reason='',wait_recorded_at=NULL,error_code='',error_detail='',updated_at=?
        WHERE run_id=? AND id=? AND status='ready' AND revision=?''',
        (attempt,generation,store.now(),run['id'],step_id,task['revision'])).rowcount:
        raise ValueError('Task no longer ready')
    if task.get('control_kind')=='PLAN' and task['spec']['profile']=='media_planner':
        db.execute("UPDATE planner_runs SET status='running',attempts=attempts+1,updated_at=? WHERE id=? AND status IN ('pending','running')",(store.now(),task['spec']['request_id']))
    db.execute('UPDATE execution_attempts SET task_revision=? WHERE id=?',(task['revision'],attempt))
    db.execute('UPDATE runs SET next_reconcile_at=? WHERE id=?',(store.now(),run['id']))


def stage_result(store,lease,outcome,data,manifest=None):
    if outcome not in ('succeeded','failed','waiting','resource_wait'):raise ValueError('Unknown task result outcome')
    if not isinstance(data,dict):raise ValueError('Task result data must be an object')
    if manifest is not None and (not isinstance(manifest,dict) or any(not isinstance(name,str) or not isinstance(record,dict) for name,record in manifest.items())):
        raise ValueError('Task result manifest must map paths to object records')
    with lease.bind(),store.connect() as db:
        lease.validate(db)
        run=graph.get(store,lease.run_id)
        if not run:raise ValueError('Workflow no longer accepts results')
        task=next((t for t in graph.work_items(run) if t['id']==lease.step_id),None)
        if run['status'] in ('CANCELLING',*graph.RUN_TERMINAL) and not (run['status']=='SUCCEEDED' and task and task.get('control_kind')=='MESSAGE'):
            raise ValueError('Workflow no longer accepts results')
        if not task or task['lease_id']!=lease.attempt or task['lease_generation']!=lease.generation:raise ValueError('Result lease replaced')
        previous=db.execute('SELECT * FROM task_results WHERE run_id=? AND task_id=? AND task_revision=? AND lease_generation=?',
                            (run['id'],task['id'],task['revision'],lease.generation)).fetchone()
        raw=graph.encode(data);files=graph.encode(manifest or {})
        if previous:
            if (previous['outcome'],previous['result_json'],previous['artifact_manifest'])!=(outcome,raw,files):raise ValueError('Result already staged with different content')
            return previous['id']
        import uuid
        identifier=uuid.uuid4().hex
        db.execute('''INSERT INTO task_results(id,run_id,task_id,task_revision,lease_id,lease_generation,outcome,
            result_json,artifact_manifest,created_at) VALUES (?,?,?,?,?,?,?,?,?,?)''',
            (identifier,run['id'],task['id'],task['revision'],lease.attempt,lease.generation,outcome,raw,files,store.now()))
        db.execute('UPDATE runs SET next_reconcile_at=? WHERE id=?',(store.now(),run['id']))
        return identifier
