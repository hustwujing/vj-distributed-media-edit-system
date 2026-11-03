"""Dynamic graph integration with the existing resource scheduler and leases."""
import json
from . import workflow_graph as graph


def candidates(store):
    now=store.now()
    return [dict(row,dynamic=True) for row in store.query('''
        SELECT w.turn_id AS id,t.status AS status,w.status AS workflow_status,w.id AS workflow,s.id AS step_id
        FROM workflow_runs w JOIN workflow_tasks s ON s.workflow_id=w.id JOIN agent_turns t ON t.id=w.turn_id
        WHERE w.status IN ('planning','running','reviewing') AND s.status='ready' AND s.lease_id='' AND s.next_run_at<=?
          AND (w.status<>'reviewing' OR s.internal=1 OR NOT EXISTS(
              SELECT 1 FROM workflow_tasks held WHERE held.workflow_id=w.id AND held.internal=0 AND held.status='paused'))
          AND t.status NOT IN ('succeeded','failed','cancelled','cancelling')
          AND NOT EXISTS(SELECT 1 FROM execution_leases l WHERE l.slot=w.turn_id||'|'||s.id AND l.active=1 AND l.expires>?)
        ORDER BY w.created_at,s.next_run_at,s.created_at LIMIT 100''',(now,now))]


def available(store,run,now):
    if run['status'] not in ('planning','running','reviewing'):return []
    leased={r['slot'] for r in store.query('SELECT slot FROM execution_leases WHERE turn_id=? AND active=1 AND expires>?',(run['turn_id'],now))}
    if run['turn_id'] in leased:return []
    done={task['id'] for task in run['tasks'] if task['status']=='succeeded'}
    control_only=run['status']=='reviewing' and any(t['status']=='paused' and not t['internal'] for t in run['tasks'])
    return [task['id'] for task in run['tasks'] if task['status']=='ready' and not task['lease_id'] and task['next_run_at']<=now
            and (not control_only or task['internal'])
            and run['turn_id']+'|'+task['id'] not in leased
            and all(edge['upstream_task_id'] in done for edge in run['dependencies'] if edge['downstream_task_id']==task['id'])]


def requirements(store,run,step_id):
    task=next((t for t in run['tasks'] if t['id']==step_id),None)
    if not task:raise ValueError('Unknown dynamic task')
    result={'cpu':1,'memory_mb':512,'gpu':False,'provider':'','step_id':step_id}
    if task['type']=='agent':
        result.update(cpu=2,memory_mb=2048);return result
    spec=graph.Task.model_validate(task['spec'])
    if spec.type=='script':result.update(cpu=2,memory_mb=2048)
    else:
        from .operations import get
        op=get(spec.operation,spec.implementation)
        result.update(cpu=op.cpu,memory_mb=op.memory_mb,gpu=op.gpu,provider=op.provider)
        if task['phase']=='poll':result.update(cpu=1,memory_mb=256,gpu=False)
    return result


def bind_claim(store,db,run,step_id,attempt,generation):
    task=next(t for t in run['tasks'] if t['id']==step_id)
    if not db.execute('''UPDATE workflow_tasks SET lease_id=?,lease_generation=?,attempts=attempts+1,updated_at=?
        WHERE workflow_id=? AND id=? AND status='ready' AND revision=?''',
        (attempt,generation,store.now(),run['id'],step_id,task['revision'])).rowcount:
        raise ValueError('Task no longer ready')
    if task['internal'] and task['spec']['profile']=='media_planner':
        db.execute("UPDATE planner_runs SET status='running',attempts=attempts+1,updated_at=? WHERE id=? AND status IN ('pending','running')",(store.now(),task['spec']['request_id']))
    db.execute('UPDATE execution_attempts SET task_revision=? WHERE id=?',(task['revision'],attempt))
    db.execute('UPDATE workflow_runs SET next_reconcile_at=? WHERE id=?',(store.now(),run['id']))


def stage_result(store,lease,outcome,data,manifest=None):
    with lease.bind(),store.connect() as db:
        lease.validate(db)
        run=graph.get(store,lease.turn_id)
        if not run or run['status'] in ('cancelling',*graph.TERMINAL):raise ValueError('Workflow no longer accepts results')
        task=next((t for t in run['tasks'] if t['id']==lease.step_id),None)
        if not task or task['lease_id']!=lease.attempt or task['lease_generation']!=lease.generation:raise ValueError('Result lease replaced')
        previous=db.execute('SELECT * FROM task_results WHERE workflow_id=? AND task_id=? AND task_revision=? AND lease_generation=?',
                            (run['id'],task['id'],task['revision'],lease.generation)).fetchone()
        raw=graph.encode(data);files=graph.encode(manifest or {})
        if previous:
            if (previous['outcome'],previous['result_json'],previous['artifact_manifest'])!=(outcome,raw,files):raise ValueError('Result already staged with different content')
            return previous['id']
        import uuid
        identifier=uuid.uuid4().hex
        db.execute('''INSERT INTO task_results(id,workflow_id,task_id,task_revision,lease_id,lease_generation,outcome,
            result_json,artifact_manifest,created_at) VALUES (?,?,?,?,?,?,?,?,?,?)''',
            (identifier,run['id'],task['id'],task['revision'],lease.attempt,lease.generation,outcome,raw,files,store.now()))
        db.execute('UPDATE workflow_runs SET next_reconcile_at=? WHERE id=?',(store.now(),run['id']))
        return identifier
