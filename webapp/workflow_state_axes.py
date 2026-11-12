"""Orthogonal workflow state projected from durable protocol records."""
from . import workflow_graph as graph


def is_external_reconcile_task(task):
    return (not task.get('control_kind') and task['phase'] in ('poll','collect')
            and bool(task.get('external_id')) and task['status'] in ('ready','leased','running','waiting','retry_wait'))


def project(store,run):
    terminal=run['status'] in graph.RUN_TERMINAL
    pending_decision=bool(store.query(
        "SELECT id FROM workflow_decisions WHERE run_id=? AND status='pending' LIMIT 1",(run['id'],)))
    external_tasks=any(is_external_reconcile_task(task) for task in graph.work_items(run))
    external_calls=bool(store.query("""SELECT request_key FROM workflow_provider_calls
        WHERE workflow=? AND state IN ('submitting','waiting','queued','running','submission_unknown') LIMIT 1""",
        (run['id'],)))
    external_cleanup=bool(store.query("""SELECT id FROM execution_external_cleanup
        WHERE task_id=? AND state IN ('pending','running','dormant') LIMIT 1""",(run['id'],)))
    controls=any(task.get('control_kind') and task['status'] in
                 ('ready','leased','running','retry_wait','reviewing','staged') for task in graph.work_items(run))
    paused=any(not task.get('control_kind') and task['status']=='paused' for task in graph.work_items(run))
    barrier=run['input_barrier_sequence'] is not None
    blocked=terminal or run['status']=='CANCELLING' or pending_decision or barrier or paused or run['terminal_reason'] in (
        'user_paused','user_rejected','authorization_convergence_failed')
    return {
        'business_admission':'TERMINAL' if terminal else 'BLOCKED' if blocked else 'OPEN',
        'user_decision':'PENDING' if pending_decision else 'NONE',
        'external_reconcile':'ACTIVE' if external_tasks or external_calls or external_cleanup else 'IDLE',
        'control_activity':'ACTIVE' if controls else 'IDLE',
        'input_barrier':'ACTIVE' if barrier else 'NONE',
    }
