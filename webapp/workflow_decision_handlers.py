"""Typed decision handlers with explicit ownership of their postconditions."""
import json
import uuid
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
    filling_initial_scope=(not run['tasks'] and run['graph_version']==0 and not store.query(
        'SELECT 1 FROM workflow_plan_tasks WHERE run_id=? LIMIT 1',(run['id'],)))
    if authorization.goal!=run['goal']:raise ValueError('Changing the top-level goal requires a new Run')
    old_contract=graph.DeliveryContract.model_validate(run['authorization'].get('delivery_contract',{})).model_dump()
    contract_changed=authorization.delivery_contract.model_dump()!=old_contract
    if run['scope_confirmed'] and not filling_initial_scope and (
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


def handle_publication(controller,run,row,request,payload,request_id,actor):
    if row['kind']!='publication_checkpoint':return False
    store=controller.store
    reviews=store.query("SELECT * FROM workflow_reviews WHERE id=? AND run_id=? AND purpose='final' AND status='passed'",
                        (request.get('review_id'),run['id']))
    if not reviews or reviews[0]['graph_version']!=run['graph_version']:
        controller.reject_decision('decision_snapshot_stale')
    if payload['action']=='approve':
        store.execute("UPDATE workflow_decisions SET status='applied',answer_json=?,updated_at=? WHERE id=?",
                      (encode(payload),store.now(),row['id']))
        controller.set_status(run,'ACTIVE','publication_approved');return True
    if payload['action']=='revise':
        if not request_id:raise ValueError('Feedback requires an idempotency request ID')
        from .run_inputs import accept
        accept(store,run['id'],actor,{'request_id':request_id,'content':payload['feedback'],
            'attachments':payload['attachments'],'source':'user'},context={
            'decision_id':row['id'],'decision_snapshot':row['snapshot_digest'],'final_review_id':reviews[0]['id']})
        store.execute('UPDATE runs SET sealed=0 WHERE id=?',(run['id'],))
        store.execute("UPDATE workflow_decisions SET status='applied',answer_json=?,updated_at=? WHERE id=?",
                      (encode(payload),store.now(),row['id']))
        controller.set_status(run,'ACTIVE','input_draining');return True
    store.execute("UPDATE workflow_decisions SET status='applied',answer_json=?,updated_at=? WHERE id=?",
                  (encode(payload),store.now(),row['id']))
    controller.set_status(run,'WAITING_USER','user_rejected');return True


def handle_external(controller,run,row,request,payload):
    if row['kind'] not in ('external_wait','external_submission'):return False
    store=controller.store
    if row['kind']=='external_wait':
        task=next(t for t in graph.work_items(run) if t['id']==request['task_id'])
        if payload['action']=='approve':
            store.execute("UPDATE run_tasks SET wait_recorded_at=?,next_run_at=? WHERE run_id=? AND id=?",
                          (store.now(),store.now(),run['id'],task['id']))
            store.execute("UPDATE workflow_decisions SET status='applied',answer_json=?,updated_at=? WHERE id=?",
                          (encode(payload),store.now(),row['id']))
            controller.set_status(run,'ACTIVE','external_wait_extended');return True
        graph.write_work(store,task,"UPDATE run_tasks SET status='paused',error_code='external_wait_timeout',error_detail=? WHERE run_id=? AND id=?",
                         ('外部任务等待超时，已放弃等待。',run['id'],task['id']))
        store.execute("UPDATE workflow_decisions SET status='applied',answer_json=?,updated_at=? WHERE id=?",
                      (encode(payload),store.now(),row['id']))
        from .task_lifecycle import request_cleanup
        with store.connect() as db:request_cleanup(db,run['id'],external=True,step_id=task['id'])
        controller.open_repair(run,task['id'],'external_wait',row['id'],'external_wait_timeout')
        store.execute('UPDATE runs SET sealed=0 WHERE id=?',(run['id'],))
        controller.set_status(run,'ACTIVE','external_wait_replan')
        controller.queue_plan(graph.get(store,run['id']),{'external_wait_abandoned':row['id'],'task_id':task['id']})
        return True
    task,call=controller.external_submission_subject(run,request)
    if payload['action']=='approve':
        external_id=str(payload.get('external_id') or '').strip()
        from .workflow_orchestrator import re_full_handle
        if not re_full_handle(external_id):raise ValueError('请填写供应商任务 ID 后再继续')
        from .operation_lifecycle import for_operation
        from .operations import get as operation
        if 'poll' not in for_operation(operation(task['spec']['operation'],task['spec']['implementation'])).protocol.phases:
            controller.reject_decision('external_submission_not_queryable')
        if call['external_id'] and call['external_id']!=external_id:
            controller.reject_decision('external_submission_id_conflict')
        store.execute("UPDATE workflow_provider_calls SET state='waiting',external_id=?,updated=? WHERE request_key=?",
                      (external_id,store.now(),call['request_key']))
        graph.write_work(store,task,"UPDATE run_tasks SET status='waiting',phase='poll',external_id=?,error_code='',error_detail='',wait_recorded_at=?,next_run_at=? WHERE run_id=? AND id=?",
                         (external_id,store.now(),store.now(),run['id'],task['id']))
        store.execute("UPDATE workflow_decisions SET status='applied',answer_json=?,updated_at=? WHERE id=?",
                      (encode(payload),store.now(),row['id']))
        controller.set_status(run,'ACTIVE','external_submission_attached');return True
    if call['external_id']:controller.reject_decision('external_submission_has_known_id')
    from .operation_lifecycle import for_operation
    from .operations import get as operation
    phase=for_operation(operation(task['spec']['operation'],task['spec']['implementation'])).protocol.state_phases['pending']
    store.execute("UPDATE workflow_provider_calls SET request_key=request_key||?,state='superseded',updated=? WHERE request_key=?",
                  (':superseded:'+uuid.uuid4().hex,store.now(),call['request_key']))
    graph.write_work(store,task,"UPDATE run_tasks SET status='retry_wait',phase=?,external_id='',error_code='',error_detail='',next_run_at=? WHERE run_id=? AND id=?",
                     (phase,store.now(),run['id'],task['id']))
    store.execute("UPDATE workflow_decisions SET status='applied',answer_json=?,updated_at=? WHERE id=?",
                  (encode(payload),store.now(),row['id']))
    controller.set_status(run,'ACTIVE','external_submission_resubmit');return True


def handle_successor(controller,run,row,request,payload,actor):
    if row['kind']!='new_run' or payload.get('action')!='approve':return False
    store=controller.store
    from .run_inputs import accept,finish_batch
    if not payload.get('authorization'):raise ValueError('Successor requires explicit scope approval')
    authorization=planning.rebind_scope(store,payload['authorization'])
    if authorization.goal!=request['new_goal']:raise ValueError('Successor goal changed after the decision')
    if (store.query('SELECT attempt FROM execution_leases WHERE run_id=? AND active=1 AND expires>?',(run['id'],store.now()))
            or store.query("SELECT attempt FROM execution_cleanup WHERE task_id=? AND state<>'complete'",(run['id'],))):
        raise ValueError('Successor must wait for the original Run to drain')
    if store.query("SELECT request_key FROM workflow_provider_calls WHERE workflow=? AND state NOT IN ('succeeded','failed','cancelled','rejected')",(run['id'],)):
        raise ValueError('Successor cannot abandon an unresolved external call')
    controller.set_status(run,'CANCELLED','successor_confirmed')
    store.execute("UPDATE run_tasks SET status='cancelled' WHERE run_id=? AND selected=1 AND status NOT IN ('succeeded','failed','cancelled')",(run['id'],))
    successor=graph.create(store,run['project_id'],None,{**authorization.model_dump(),'require_plan_confirmation':True},
        parent_run_id=run['id'],reconcile_immediately=False)
    cutoff=int(request.get('input_through_sequence',0))
    for item in store.query("SELECT * FROM run_inputs WHERE run_id=? AND status IN ('ROUTING','ACCEPTED','PLANNING') ORDER BY sequence",(run['id'],)):
        carried=accept(store,successor['id'],actor,{'request_id':item['id'],'content':item['content'],
            'attachments':[a['id'] for a in json.loads(item['attachments_json'])]},context={
            'predecessor_input_id':item['id'],**({'message_disposition':'replan'} if item['status']!='ROUTING' else {})},
            defer_reconcile=True)
        if item['status']!='ROUTING' and item['sequence']<=cutoff:
            now=store.now()
            store.execute("UPDATE run_inputs SET status='APPLIED',applied_at=?,applied_graph_version=? WHERE id=?",
                          (now,successor['graph_version'],carried['id']))
            graph.event(store,successor,'successor_input_applied',{
                'input_id':carried['id'],'predecessor_input_id':item['id'],'input_sequence':carried['sequence']})
        store.execute("UPDATE run_inputs SET status='SUPERSEDED',successor_run_id=?,applied_at=? WHERE id=?",
                      (successor['id'],store.now(),item['id']))
    finish_batch(store,graph.get(store,successor['id']),cutoff)
    store.execute('UPDATE runs SET next_reconcile_at=?,updated_at=? WHERE id=?',(store.now(),store.now(),successor['id']))
    store.execute('UPDATE runs SET input_barrier_sequence=NULL WHERE id=?',(run['id'],))
    store.execute("UPDATE control_jobs SET status='cancelled',updated_at=? WHERE run_id=? AND control_kind='MESSAGE' AND status NOT IN ('succeeded','failed','cancelled')",
                  (store.now(),run['id']))
    store.execute("UPDATE workflow_decisions SET status='superseded' WHERE run_id=? AND status='pending' AND id<>?",(run['id'],row['id']))
    store.execute("UPDATE workflow_decisions SET status='applied',answer_json=?,updated_at=? WHERE id=?",
                  (encode({**payload,'successor_run_id':successor['id']}),store.now(),row['id']))
    graph.event(store,run,'successor_created',{'successor_run_id':successor['id']})
    return True


def handle_delivery_scope(controller,run,row,request,payload):
    if row['kind']!='delivery_scope_change' or payload.get('action')!='approve':return False
    store=controller.store
    try:authorization=planning.rebind_scope(store,payload.get('authorization'))
    except ValueError as exc:controller.reject_decision('scope_invalid',str(exc))
    if authorization.goal!=run['goal']:raise ValueError('Delivery scope change cannot change the top-level goal')
    old_targets=set(run['authorization'].get('delivery_targets',[]));new_targets=set(authorization.delivery_targets)
    if not new_targets:raise ValueError('Delivery scope cannot be empty after confirmation')
    if not new_targets<=old_targets:raise ValueError('Delivery scope expansion requires a new explicit plan change')
    old_contract=graph.DeliveryContract.model_validate(run['authorization'].get('delivery_contract',{}));new_contract=authorization.delivery_contract
    if (not set(new_contract.filenames)<=set(old_contract.filenames)
            or not set(new_contract.formats)<=set(old_contract.formats)
            or not set(new_contract.roles)<=set(old_contract.roles)
            or not set(old_contract.excluded_filenames)<=set(new_contract.excluded_filenames)):
        raise ValueError('Delivery contract expansion requires a new explicit plan change')
    from .run_authorization import approve
    approve(store,run,authorization)
    superseded=store.query("SELECT task_id FROM workflow_reviews WHERE run_id=? AND purpose='final' AND status IN ('passed','rejected','pending','running','staged')",(run['id'],))
    store.execute("UPDATE workflow_reviews SET status='superseded',updated_at=? WHERE run_id=? AND purpose='final' AND status IN ('passed','rejected','pending','running','staged')",
                  (store.now(),run['id']))
    from .task_lifecycle import request_cleanup
    with store.connect() as db:
        for old in superseded:
            db.execute("UPDATE control_jobs SET status='cancelled',lease_id='',lease_generation=0,updated_at=? WHERE run_id=? AND id=? AND status NOT IN ('succeeded','failed','cancelled')",
                       (store.now(),run['id'],old['task_id']))
            request_cleanup(db,run['id'],external=False,step_id=old['task_id'])
    store.execute("UPDATE workflow_decisions SET status='applied',answer_json=?,updated_at=? WHERE id=?",
                  (encode(payload),store.now(),row['id']))
    current=graph.get(store,run['id']);controller.set_status(current,'ACTIVE','delivery_scope_confirmed')
    controller.queue_review(graph.get(store,run['id']))
    return True


def quality_subject(store,run,row,request):
    if row['kind'] not in ('quality','quality_feedback'):return None
    from .run_inputs import candidate_digest
    subject=next(t for t in run['tasks'] if t['id']==request['task_id'])
    if subject['revision']!=request['revision'] or request['candidate_digest']!=candidate_digest(store,run,subject):
        raise ValueError('Candidate changed after the decision snapshot')
    return subject


def handle_feedback(controller,run,row,request,payload,request_id,actor,subject):
    if payload.get('action')!='revise':return False
    if row['kind'] not in ('quality','quality_feedback','input_question','plan_confirmation'):
        raise ValueError('Decision does not support feedback')
    if not request_id:raise ValueError('Feedback requires an idempotency request ID')
    store=controller.store
    batch=payload.get('input_batch') or {}
    feedback=str(payload.get('feedback') or batch.get('content') or '')
    attachments=list(payload.get('attachments') or batch.get('attachments') or [])
    upstream_tasks=list(payload.get('upstream_tasks') or batch.get('targets') or [])
    from .run_inputs import accept,revision_closure
    context={'decision_id':row['id'],'decision_snapshot':row['snapshot_digest']}
    if row['kind']=='plan_confirmation':
        from .workflow_conversation import invalidate_plan
        context['superseded_plan']={key:request[key] for key in ('reason','delivery_targets','delivery_contract','steps','dependencies')}
        invalidate_plan(store,run,row)
    if row['kind'] not in ('input_question','plan_confirmation'):
        if subject['user_revision_count']>=5:raise ValueError('User revision limit reached')
        context.update(subject_task=subject['id'],subject_revision=subject['revision'],
            candidate_digest=request['candidate_digest'],candidate=subject['candidate_result'],quality=subject['quality'],
            allowed_revision_tasks=revision_closure(run,subject['id'],upstream_tasks))
        context['review_evidence']=[json.loads(item['decision_json']) for item in store.query(
            "SELECT decision_json FROM workflow_reviews WHERE run_id=? AND subject_task=? ORDER BY created_at DESC LIMIT 5",
            (run['id'],subject['id']))]
        store.execute("UPDATE run_tasks SET status='paused',error_code='user_revision_requested' WHERE run_id=? AND id=?",
                      (run['id'],subject['id']))
    invalid_inputs=request.get('invalid_input_ids',[]) if row['kind']=='input_question' else []
    if invalid_inputs:
        context['superseded_inputs']=[{'id':item['id'],'content':item['content'],'context':json.loads(item['context_json'])}
            for item in store.query("SELECT id,content,context_json FROM run_inputs WHERE run_id=? AND status='ACCEPTED'",(run['id'],))
            if item['id'] in invalid_inputs]
    accept(store,run['id'],actor,{'request_id':request_id,'content':feedback,
        'attachments':attachments,'source':'user' if row['kind'] in ('input_question','plan_confirmation') else 'quality_feedback'},context=context)
    if invalid_inputs:
        from .run_inputs import supersede_inputs
        supersede_inputs(store,run,invalid_inputs,'attachment_replaced_by_user')
    store.execute("UPDATE workflow_decisions SET status='applied',answer_json=?,updated_at=? WHERE id=?",
                  (encode(payload),store.now(),row['id']))
    controller.set_status(run,'ACTIVE','input_draining')
    return True


def handle_rejection(controller,run,row,request,payload,subject):
    if payload.get('action')!='reject':return False
    store=controller.store
    if row['kind']=='plan_confirmation':
        from .workflow_conversation import invalidate_plan
        from .run_inputs import reject_batch
        invalidate_plan(store,run,row);reject_batch(store,run,int(request.get('input_through_sequence') or 0),'user_rejected_plan')
        store.execute("UPDATE workflow_decisions SET status='applied',answer_json=?,updated_at=? WHERE id=?",
                      (encode(payload),store.now(),row['id']))
        controller.set_status(run,'WAITING_USER','user_rejected');return True
    if row['kind'] in ('plan_change','successor_authorization'):
        if request.get('planner_run'):
            store.execute("UPDATE planner_runs SET status='rejected',error_code='authorization_rejected',updated_at=? WHERE id=? AND run_id=? AND status='awaiting_authorization'",
                          (store.now(),request['planner_run'],run['id']))
            store.execute("UPDATE workflow_plan_drafts SET status='REJECTED',updated_at=? WHERE planning_request_id=? AND status IN ('OPEN','COMMITTED')",
                          (store.now(),request['planner_run']))
        cutoff=int(request.get('input_through_sequence') or 0)
        if cutoff:
            from .run_inputs import reject_batch
            reject_batch(store,run,cutoff);controller.set_status(run,'ACTIVE','authorization_rejected')
        else:controller.set_status(run,'FAILED','authorization_rejected')
    elif row['kind']=='input_question':
        from .run_inputs import reject_batch,supersede_inputs
        cutoff=int(request.get('input_through_sequence') or 0);invalid_inputs=request.get('invalid_input_ids',[])
        if cutoff:reject_batch(store,run,cutoff,'user_rejected_input')
        elif invalid_inputs:supersede_inputs(store,run,invalid_inputs,'user_rejected_input')
        remaining=store.query("SELECT id FROM run_inputs WHERE run_id=? AND sequence>? AND status='ACCEPTED' LIMIT 1",(run['id'],cutoff))
        continue_work=bool(remaining or run['tasks'])
        controller.set_status(run,'ACTIVE' if continue_work else 'WAITING_USER','input_draining' if continue_work else 'user_rejected')
    elif row['kind'] in ('new_run','delivery_scope_change'):
        controller.set_status(run,'WAITING_USER','user_rejected')
    elif row['kind'] in ('quality','quality_feedback'):
        store.execute("UPDATE run_tasks SET status='paused',error_code='quality_failed',error_detail=? WHERE run_id=? AND id=?",
                      ('用户未通过内容验收，请调整要求后重新规划。',run['id'],subject['id']))
        controller.set_status(run,'WAITING_USER','user_rejected')
    else:raise ValueError('Decision does not support rejection')
    store.execute("UPDATE workflow_decisions SET status='applied',answer_json=?,updated_at=? WHERE id=?",
                  (encode(payload),store.now(),row['id']))
    return True


def handle_quality_approval(controller,run,row,request,payload,subject):
    if row['kind']!='quality' or payload.get('action')!='approve':return False
    store=controller.store;task=subject
    if not any(item['kind']=='review' for item in task['spec']['acceptance']):raise ValueError('User review was not declared for this Task')
    if task['revision']!=request['revision'] or not task['quality'].get('technical_valid'):raise ValueError('Technical acceptance failed')
    if task['id'] in controller.pending_task_reviews(run):raise ValueError('Agent review is still pending')
    if task['quality'].get('requires_agent') and not task['quality'].get('agent_review_passed'):
        raise ValueError('Agent review has not passed')
    store.execute("UPDATE run_tasks SET status='succeeded',accepted_result_json=candidate_result_json,error_code='',error_detail='',wait_reason='' WHERE run_id=? AND id=?",
                  (run['id'],task['id']))
    store.execute("UPDATE workflow_decisions SET status='applied',answer_json=?,updated_at=? WHERE id=?",
                  (encode(payload),store.now(),row['id']))
    controller.set_status(run,'ACTIVE')
    return True
