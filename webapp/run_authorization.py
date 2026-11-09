"""Versioned scope grants; expansion never rewrites a Task's frozen grant."""
from .workflow_graph import encode


def approve(store,run,authorization):
    old=run['authorization'];new=authorization.model_dump()
    if run['scope_confirmed'] and old['delivery_targets'] and not new['delivery_targets']:raise ValueError('A confirmed delivery list cannot be cleared; stop the task instead')
    new['require_plan_confirmation']=old.get('require_plan_confirmation',False) or not run['scope_confirmed']
    if run['scope_confirmed']:
        for key in ('allowed_assets','allowed_operations','allowed_agent_profiles'):
            if not set(old[key])<=set(new[key]):raise ValueError('Scope expansion cannot revoke previously granted capabilities; stop the Run to revoke them')
    for row in store.query("SELECT category,used FROM execution_budgets WHERE run_id=? AND category LIKE 'provider:%'",(run['id'],)):
        group=row['category'].split(':',1)[1]
        if new['provider_call_budget'].get(group,0)<row['used']:raise ValueError('Provider budget cannot discard prior usage')
    used=store.query('SELECT COALESCE(SUM(COALESCE(used_tokens,reserved_tokens)),0) AS n FROM workflow_model_reservations WHERE run_id=?',(run['id'],))[0]['n']
    if new['model_token_budget']<used:raise ValueError('Model budget cannot discard prior usage or unresolved reservations')
    plans=store.query('SELECT COUNT(*) AS n FROM planner_runs WHERE run_id=?',(run['id'],))[0]['n']
    if new['max_planning_rounds']<plans:raise ValueError('Planning budget cannot discard prior rounds')
    limits=store.query('SELECT used_seconds FROM execution_limits WHERE task_id=?',(run['id'],))[0]
    if new['wall_time_budget']<limits['used_seconds']:raise ValueError('Execution budget cannot discard prior usage')
    version=run['authorization_version']+1
    store.execute('INSERT INTO run_authorizations VALUES (?,?,?,?)',(run['id'],version,encode(new),store.now()))
    store.execute('UPDATE runs SET authorization_json=?,authorization_version=?,scope_confirmed=1,goal=? WHERE id=?',
        (encode(new),version,new['goal'],run['id']))
    store.execute('UPDATE execution_limits SET deadline_at=?,budget_seconds=? WHERE task_id=?',
        (run['created_at']+new['wall_time_budget'],new['wall_time_budget'],run['id']))
    return version
