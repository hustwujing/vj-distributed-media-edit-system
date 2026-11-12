"""Typed decision handlers with explicit ownership of their postconditions."""
import json
from . import workflow_graph as graph,workflow_planning as planning
from .workflow_graph import AuthorizationRequired,encode


def handle_approval(controller,run,row,request,payload):
    if payload.get('action')!='approve':return False
    store=controller.store
    if row['kind']=='plan_confirmation':
        if run['input_sequence']!=request['input_sequence']:controller.reject_decision('decision_snapshot_stale')
        if graph.content_fingerprint(store,run['project_id'])!=request['content_fingerprint']:
            controller.reject_decision('content_changed')
        plans=store.query("SELECT * FROM planner_runs WHERE id=? AND run_id=? AND status='awaiting_confirmation'",
                          (request['planning_request_id'],run['id']))
        if not plans or graph.digest(json.loads(plans[0]['proposal_json']))!=request['proposal_digest']:
            controller.reject_decision('decision_snapshot_stale')
        plan=plans[0]
        store.execute("UPDATE planner_runs SET status='staged' WHERE id=?",(plan['id'],))
        controller.set_status(run,'ACTIVE','plan_confirmed')
        planning.apply(store,graph.get(store,run['id']),{**plan,'status':'staged'},confirmed_plan=True)
        store.execute("UPDATE workflow_decisions SET status='applied',answer_json=?,updated_at=? WHERE id=?",
                      (encode(payload),store.now(),row['id']))
        return True
    if row['kind'] not in ('plan_change','successor_authorization'):return False
    try:authorization=planning.rebind_scope(store,payload.get('authorization'))
    except ValueError as exc:controller.reject_decision('scope_invalid',str(exc))
    filling_initial_targets=not run['tasks'] and run['graph_version']==0 and not run['authorization']['delivery_targets']
    if authorization.goal!=run['goal']:raise ValueError('Changing the top-level goal requires a new Run')
    old_contract=graph.DeliveryContract.model_validate(run['authorization'].get('delivery_contract',{})).model_dump()
    contract_changed=authorization.delivery_contract.model_dump()!=old_contract
    if run['scope_confirmed'] and not filling_initial_targets and (
            set(authorization.delivery_targets)!=set(run['authorization']['delivery_targets']) or contract_changed):
        cutoff=int(request.get('input_through_sequence',0))
        if not cutoff or not store.query("SELECT id FROM run_inputs WHERE run_id=? AND sequence<=? AND status IN ('ACCEPTED','PLANNING')",
                                         (run['id'],cutoff)):
            raise ValueError('Delivery contract change requires explicit pending user input')
    owned={asset['id'] for asset in store.assets(run['project_id'])}
    if not set(authorization.allowed_assets)<=owned:raise ValueError('Foreign asset')
    if authorization.content_fingerprint!=graph.content_fingerprint(store,run['project_id']):
        controller.reject_decision('content_changed')
    from .run_authorization import approve
    approve(store,run,authorization)
    store.execute("UPDATE workflow_decisions SET status='superseded',updated_at=? WHERE run_id=? AND id<>? AND status='pending'",
                  (store.now(),run['id'],row['id']))
    store.execute("UPDATE workflow_decisions SET status='applied',answer_json=?,updated_at=? WHERE id=?",
                  (encode(payload),store.now(),row['id']))
    controller.set_status(run,'ACTIVE')
    current=graph.get(store,run['id'])
    if row['kind']=='plan_change' and request.get('planner_run'):
        try:restaged=planning.restage_after_authorization(
            store,current,request['planner_run'],request.get('proposal_digest',''))
        except AuthorizationRequired as exc:
            controller.authorization_convergence_failed(current,exc);return True
        except ValueError:restaged=False
        if restaged:
            controller.set_status(current,'ACTIVE','authorization_proposal_resumed')
            return True
    controller.queue_plan(current,{'authorization_changed':row['id']})
    return True
