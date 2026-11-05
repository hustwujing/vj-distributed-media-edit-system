"""Dynamic graph integration with the existing resource scheduler and leases."""
import json
from . import workflow_graph as graph


def candidates(store):
    now=store.now()
    return [dict(row,dynamic=True) for row in store.query('''
        SELECT w.id AS id,t.status AS status,w.status AS workflow_status,w.id AS workflow,s.id AS step_id
        FROM runs w JOIN run_work_items s ON s.run_id=w.id JOIN execution_subjects t ON t.id=w.id
        WHERE (w.status='ACTIVE' OR (w.status='WAITING_USER' AND w.input_barrier_sequence IS NOT NULL AND w.terminal_reason NOT IN ('user_paused','user_rejected')))
          AND (w.scope_confirmed=1 OR s.control_kind<>'')
          AND s.selected=1 AND s.status='ready' AND s.lease_id='' AND s.next_run_at<=?
          AND (w.input_barrier_sequence IS NOT NULL OR s.control_kind<>'' OR s.purpose IN ('repair','evidence')
              OR s.repair_count>0 OR s.user_revision_count>0 OR NOT EXISTS(
              SELECT 1 FROM run_tasks held WHERE held.run_id=w.id AND held.selected=1 AND held.status='paused'))
          AND (w.input_barrier_sequence IS NULL OR s.control_kind<>'' OR EXISTS(SELECT 1 FROM run_input_drain d WHERE d.run_id=w.id AND d.task_id=s.id AND d.revision=s.revision))
          AND t.status NOT IN ('succeeded','failed','cancelled','cancelling')
          AND NOT EXISTS(SELECT 1 FROM execution_leases l WHERE l.slot=w.id||'|'||s.id AND l.active=1 AND l.expires>?)
        ORDER BY w.created_at,s.next_run_at,s.created_at LIMIT 100''',(now,now))]


def available(store,run,now):
    if run['status']!='ACTIVE' and not (run['status']=='WAITING_USER' and run['input_barrier_sequence'] is not None and run['terminal_reason'] not in ('user_paused','user_rejected')):return []
    leased={r['slot'] for r in store.query('SELECT slot FROM execution_leases WHERE run_id=? AND active=1 AND expires>?',(run['id'],now))}
    if run['id'] in leased:return []
    done={task['id'] for task in graph.work_items(run) if task['status']=='succeeded'}
    control_only=run['input_barrier_sequence'] is None and any(t['status']=='paused' and not t.get('control_kind') for t in graph.work_items(run))
    from .run_inputs import can_dispatch
    return [task['id'] for task in graph.work_items(run) if (run['scope_confirmed'] or task.get('control_kind')) and can_dispatch(run,task) and task['status']=='ready' and not task['lease_id'] and task['next_run_at']<=now
            and (not control_only or task.get('control_kind') or task['purpose'] in ('repair','evidence')
                 or task.get('repair_count',0)>0 or task.get('user_revision_count',0)>0)
            and run['id']+'|'+task['id'] not in leased
            and all(edge['upstream_task_id'] in done for edge in run['dependencies'] if edge['downstream_task_id']==task['id'])]


def requirements(store,run,step_id):
    task=next((t for t in graph.work_items(run) if t['id']==step_id),None)
    if not task:raise ValueError('Unknown dynamic task')
    result={'cpu':1,'memory_mb':512,'gpu':False,'provider':'','step_id':step_id}
    if task['type']=='agent':
        result.update(cpu=2,memory_mb=2048);return result
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
    if not db.execute(f'''UPDATE {graph.work_table(task)} SET lease_id=?,lease_generation=?,attempts=attempts+1,updated_at=?
        WHERE run_id=? AND id=? AND status='ready' AND revision=?''',
        (attempt,generation,store.now(),run['id'],step_id,task['revision'])).rowcount:
        raise ValueError('Task no longer ready')
    if task.get('control_kind') and task['spec']['profile']=='media_planner':
        db.execute("UPDATE planner_runs SET status='running',attempts=attempts+1,updated_at=? WHERE id=? AND status IN ('pending','running')",(store.now(),task['spec']['request_id']))
    db.execute('UPDATE execution_attempts SET task_revision=? WHERE id=?',(task['revision'],attempt))
    db.execute('UPDATE runs SET next_reconcile_at=? WHERE id=?',(store.now(),run['id']))


def stage_result(store,lease,outcome,data,manifest=None):
    with lease.bind(),store.connect() as db:
        lease.validate(db)
        run=graph.get(store,lease.run_id)
        if not run or run['status'] in ('CANCELLING',*graph.RUN_TERMINAL):raise ValueError('Workflow no longer accepts results')
        task=next((t for t in graph.work_items(run) if t['id']==lease.step_id),None)
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
