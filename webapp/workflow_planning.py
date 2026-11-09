"""Frozen planning requests and atomic, idempotent DAG proposal application."""
import json
import os
import uuid
from .workflow_graph import Authorization,Proposal,ManagerResult,Task,encode,digest,get,event,validate_proposal,initial_phase,active_tasks


class ProposalIdentityError(ValueError):pass
class ProposalReferenceError(ValueError):pass
class ProposalSnapshotError(ProposalReferenceError):pass
class PlanningBudgetExceeded(ValueError):pass
class PlanningScopeRequired(ProposalReferenceError):code='scope_confirmation_required'


def validate_references(store,run,proposal):
    known={asset['id'] for asset in store.assets(run['project_id'])}
    if proposal and any(ref.asset and ref.asset not in known for task in proposal.add_tasks for ref in task.inputs.values()):
        raise ProposalReferenceError('Unknown input asset in this project; copy an existing asset ID exactly. Authorization cannot create a missing asset.')


def available_operations(store):
    from .operations import catalog
    rows=store.query('''SELECT c.catalogue FROM execution_operation_catalog c JOIN execution_workers w ON w.id=c.worker
        WHERE w.draining=0 AND w.heartbeat>?''',(store.now()-30,))
    if not rows:
        if os.environ.get('VIDEO_USE_STORAGE_METADATA_ONLY')=='1':return None
        return catalog()['operations']
    catalogues=[]
    for row in rows:
        try:
            value=json.loads(row['catalogue'])
            if not isinstance(value,dict) or not isinstance(value.get('operations'),list):raise ValueError('Invalid catalogue')
            items=value['operations']
            indexed={item['id']:item for item in items if isinstance(item,dict) and isinstance(item.get('id'),str)}
            if len(indexed)!=len(items) or any(not key for key in indexed):raise ValueError('Invalid operation identity')
        except (TypeError,ValueError):
            from .telemetry import emit
            emit('workflow.catalogue_unavailable','WARNING',reason='invalid_catalogue')
            return None
        catalogues.append(indexed)
    common=set(catalogues[0])
    for indexed in catalogues[1:]:common&=set(indexed)
    operations=[catalogues[0][identifier] for identifier in sorted(common)
                if len({digest(indexed[identifier]) for indexed in catalogues})==1]
    if not operations and any(catalogues):
        from .telemetry import emit
        emit('workflow.catalogue_unavailable','WARNING',reason='no_common_contracts')
        return None
    return operations


def bind_scope(store,raw):
    from .workflow_graph import bind_authorization,Authorization
    requested=Authorization.model_validate(raw)
    operations=available_operations(store)
    if not operations:raise ValueError('No worker capability catalogue is available for scope confirmation')
    bindings={operation['id']:operation['implementation'] for operation in operations}
    if any(name!='script' and name not in bindings for name in requested.allowed_operations):
        raise ValueError('An approved operation is not advertised by workers')
    policy_keys=('external_side_effects','budget_group','concurrency_group','route_adapter','provider_identity','cleanup_category','reuse_safe')
    policies={op['id']:digest({key:op[key] for key in policy_keys}) for op in operations}
    from .workflow_graph import asset_digest
    assets={}
    for identifier in requested.allowed_assets:
        rows=store.query('SELECT * FROM assets WHERE id=?',(identifier,))
        if not rows:raise ValueError('Authorized asset does not exist')
        asset={**rows[0],'metadata':json.loads(rows[0]['metadata'])};assets[identifier]=asset_digest(asset)
    if any(assets.get(key)!=value for key,value in requested.allowed_asset_digests.items()):raise ValueError('Requested asset version is stale')
    requested.allowed_asset_digests=assets
    return bind_authorization(requested.model_dump(),implementations=bindings,policies=policies)


def rebind_scope(store,raw):
    """Bind user-confirmed logical scope to current server-owned identities."""
    if not isinstance(raw,dict):raise ValueError('Authorization is required')
    logical=dict(raw)
    for key in ('allowed_asset_digests','operation_implementations','operation_policy_digests','agent_profile_digests'):
        logical[key]={}
    return bind_scope(store,logical)


def snapshot(store,run):
    from .effective_delivery import confirmed_scope
    from .workflow_conversation import context as conversation_context,can_respond
    from .specialist_profiles import catalogue as profiles
    from .repair_inputs import descriptors
    from .workflow_history import snapshot as history_snapshot,user_reviews
    assets=store.assets(run['project_id'])
    permitted=set(run['authorization']['allowed_assets'])
    operations=available_operations(store)
    if operations is None:return None
    diagnostics={}
    for row in store.query('SELECT step,record FROM workflow_diagnostics WHERE workflow=? ORDER BY created DESC LIMIT 768',(run['id'],)):
        values=diagnostics.setdefault(row['step'],[])
        if len(values)<3:values.append(json.loads(row['record']))
    feedback=[{**{key:row[key] for key in ('id','purpose','subject_task','graph_version','status')},
               'decision':json.loads(row['decision_json'])} for row in store.query(
        "SELECT id,purpose,subject_task,graph_version,status,decision_json FROM workflow_reviews WHERE run_id=? AND status='rejected' AND decision_json<>'{}' ORDER BY updated_at DESC LIMIT 10",(run['id'],))]
    applied_inputs=store.query("SELECT sequence,content,source,context_json FROM run_inputs WHERE run_id=? AND status='APPLIED' ORDER BY sequence DESC LIMIT 101",(run['id'],))
    for change in applied_inputs:change['context']=json.loads(change.pop('context_json'))
    reservations=store.query('SELECT COALESCE(SUM(COALESCE(used_tokens,reserved_tokens)),0) AS used FROM workflow_model_reservations WHERE run_id=?',(run['id'],))[0]['used']
    provider_used={}
    for call in store.query('SELECT implementation FROM workflow_provider_calls WHERE workflow=?',(run['id'],)):
        group=json.loads(call['implementation'])['budget_group']
        provider_used[group]=provider_used.get(group,0)+1
    operation_index=[{'id':op['id'],'authorized':op['id'] in run['authorization']['allowed_operations'],
        'available':op.get('available',False),**{key:op[key] for key in ('external_side_effects','budget_group','concurrency_group','route_adapter','provider_identity','cleanup_category')},'implementation':op.get('implementation'),
        'outputs':op.get('output_contract',{}).get('roles',[]),'output_contract':op.get('output_contract',{}),
        'constraints':op.get('constraints'),
        'contract_digest':digest(op)} for op in operations]
    media_constraints=[]
    for task in active_tasks(run):
        accepted=set(task.get('accepted_result',[]));roles=task.get('quality',{}).get('output_roles',{})
        for item in task.get('quality',{}).get('files',[]):
            if item.get('name') not in accepted or item.get('kind')!='media':continue
            media_constraints.append({'task_id':task['id'],'revision':task['revision'],'reference':item['name'],
                'roles':[role for role,path in roles.items() if path==item['name']],
                **{key:item.get(key) for key in ('duration','width','height','fps','video','audio_track_present')}})
    return {'run_id':run['id'],'goal':run['goal'],'graph_version':run['graph_version'],
        'effective_delivery_scope':confirmed_scope(store,run),
        'conversation_context':conversation_context(store,run),'can_respond':can_respond(run),
        'scope_confirmation_required':not run['scope_confirmed'],
        'sealed':run['sealed'],'authorization':run['authorization'],'authorization_version':run['authorization_version'],
        'repair_context':run.get('repair_context',{}),'repair_candidates':descriptors(store,run),
        'history':history_snapshot(store,run),'user_reviews':user_reviews(store,run),
        'user_changes':list(reversed(applied_inputs[:100])),'user_changes_truncated':len(applied_inputs)>100,
        'tasks':[{**{k:task[k] for k in ('id','spec','status','revision','repair_count','user_revision_count','review_again_count','created_graph_version','authorization_version','accepted_result','quality','error_code','error_detail')},
                  'bindings':{role:{key:binding[key] for key in ('asset','sha256','step','revision') if key in binding}
                              for role,binding in task.get('bindings',{}).items() if role in task['spec'].get('inputs',{}) and isinstance(binding,dict)}}
                 for task in active_tasks(run)],
        'dependencies':run['dependencies'],'assets':[a for a in assets if a['id'] in permitted],
        'upstream_media_constraints':media_constraints,
        'diagnostics':diagnostics,'diagnostics_truncated':len(store.query('SELECT step FROM workflow_diagnostics WHERE workflow=? LIMIT 769',(run['id'],)))>768,
        'review_feedback':feedback,'review_feedback_truncated':len(store.query("SELECT id FROM workflow_reviews WHERE run_id=? AND status='rejected' LIMIT 11",(run['id'],)))>10,
        'budget_state':{'model_tokens':{'limit':run['authorization']['model_token_budget'],'used_or_reserved':reservations,
                         'remaining':max(0,run['authorization']['model_token_budget']-reservations)},
                        'planning_rounds':{'limit':run['authorization']['max_planning_rounds'],'used':len(store.query('SELECT id FROM planner_runs WHERE run_id=?',(run['id'],)))},
                        'provider_calls':{name:{'limit':limit,'used':provider_used.get(name,0),'remaining':max(0,limit-provider_used.get(name,0))}
                            for name,limit in run['authorization']['provider_call_budget'].items()}},
        # Discoverability is not authorization. A Manager must know the actual
        # contract to request an explicit scope change; apply() still gates it.
        'operations':operation_index,
        'profiles':[{**p,'authorized':p['id'] in run['authorization']['allowed_agent_profiles']} for p in profiles()
                    if p['id'] not in ('media_planner','delivery_reviewer')],
        'proposal_schema':ManagerResult.model_json_schema()}


def operation_contract(store,frozen,identifier):
    indexed=next((item for item in frozen.get('operations',[]) if item['id']==identifier),None)
    if not indexed:raise ValueError('Operation is outside the frozen capability index')
    current=next((item for item in (available_operations(store) or []) if item['id']==identifier),None)
    if not current or digest(current)!=indexed['contract_digest']:
        raise ValueError('Operation contract changed after the planning snapshot')
    return {**current,'authorized':indexed['authorized'],'contract_digest':indexed['contract_digest']}


def queue(store,run,trigger):
    """Called in the orchestrator's fenced transaction."""
    if run['input_barrier_sequence'] is not None and 'input_batch' not in trigger:return None
    current=store.query("SELECT id FROM planner_runs WHERE run_id=? AND status IN ('pending','running','staged','awaiting_confirmation')",(run['id'],))
    if current:return current[0]['id']
    count=store.query('SELECT COUNT(*) AS n FROM planner_runs WHERE run_id=?',(run['id'],))[0]['n']
    if count>=run['authorization']['max_planning_rounds']:raise PlanningBudgetExceeded('Planning round budget exhausted')
    identifier=uuid.uuid4().hex;task_id=identifier;now=store.now()
    frozen=snapshot(store,run)
    if frozen is None:
        store.execute("UPDATE runs SET status='ACTIVE',terminal_reason='workers_unavailable',next_reconcile_at=? WHERE id=?",
                      (store.now()+5,run['id']))
        event(store,run,'planning_deferred',{'reason':'workers_unavailable'})
        return None
    frozen['trigger']=trigger
    frozen['input_batch']=trigger.get('input_batch')
    frozen['planning_request_id']=identifier
    frozen['used_proposal_ids']=[row['proposal_id'] for row in store.query('SELECT proposal_id FROM planner_runs WHERE run_id=? AND proposal_id IS NOT NULL',(run['id'],))]
    spec={'profile':'media_planner','objective':run['goal'],'request_id':identifier,'snapshot_digest':digest(frozen)}
    store.execute('''INSERT INTO planner_runs(id,run_id,task_id,base_graph_version,authorization_version,
        trigger_json,snapshot_json,snapshot_digest,status,created_at,updated_at) VALUES (?,?,?,?,?,?,?,?,'pending',?,?)''',
        (identifier,run['id'],task_id,run['graph_version'],run['authorization_version'],encode(trigger),encode(frozen),digest(frozen),now,now))
    from .workflow_plan_draft import create as create_draft
    create_draft(store,identifier,run['id'],frozen,now)
    from .specialist_profiles import get as profile
    store.execute('''INSERT INTO control_jobs(run_id,id,type,authorization_version,control_kind,snapshot_json,snapshot_digest,spec_json,profile_json,status,created_graph_version,created_at,updated_at)
        VALUES (?,?,'agent',?,'PLAN',?,?,?,?,'ready',?,?,?)''',(run['id'],task_id,run['authorization_version'],encode(frozen),digest(frozen),encode(spec),encode(profile('media_planner').snapshot()),run['graph_version'],now,now))
    store.execute("UPDATE runs SET status='ACTIVE',terminal_reason=CASE WHEN terminal_reason='workers_unavailable' THEN '' ELSE terminal_reason END,updated_at=? WHERE id=?",(now,run['id']))
    if run.get('terminal_reason')=='workers_unavailable':event(store,run,'workers_recovered')
    event(store,run,'planning_requested',{'request_id':identifier,'round':count+1})
    return identifier


def stage(store,lease,request_id,raw,*,draft_revision=None,draft_digest=None):
    result=ManagerResult.model_validate(raw)
    proposal=result.proposal
    with lease.bind(),store.connect() as db:
        lease.validate(db)
        request=db.execute('SELECT * FROM planner_runs WHERE id=?',(request_id,)).fetchone()
        if not request or request['task_id']!=lease.step_id:raise ValueError('Planner request outside execution scope')
        run=get(store,request['run_id'])
        if run['id']!=lease.run_id or request['status'] not in ('pending','running','staged'):raise ValueError('Planner request is no longer active')
        frozen=json.loads(request['snapshot_json'])
        if result.kind=='RESPOND':
            from .workflow_conversation import can_respond
            if not can_respond(run):raise ValueError('RESPOND cannot bypass business execution or review')
        if frozen.get('scope_confirmation_required') and result.kind not in ('REQUEST_AUTHORIZATION','REQUEST_USER_INPUT','NEW_RUN_REQUIRED','RESPOND'):
            raise PlanningScopeRequired('This successor has no confirmed business scope. Propose a complete authorization and delivery targets before any execution.')
        if result.kind=='REQUEST_AUTHORIZATION' and result.authorization.goal!=run['goal']:
            raise PlanningScopeRequired('Copy the frozen goal exactly for scope expansion; use NEW_RUN_REQUIRED for a different top-level goal')
        if result.proposal and run['authorization'].get('require_plan_confirmation') and not run['authorization']['delivery_targets'] and not result.delivery_targets:
            raise PlanningScopeRequired('Supply delivery_targets with the proposed concrete outputs before asking the user to confirm the plan')
        cutoff=(frozen.get('input_batch') or {}).get('through_sequence',0)
        if (result.delivery_targets and run['authorization']['delivery_targets']
                and set(result.delivery_targets)!=set(run['authorization']['delivery_targets']) and not cutoff):
            raise PlanningScopeRequired('A delivery-list change requires a frozen explicit user input and plan confirmation')
        expected={'snapshot_digest':request['snapshot_digest'],'base_graph_version':request['base_graph_version'],
                  'authorization_version':request['authorization_version'],'input_through_sequence':cutoff}
        mismatched=[name for name,value in expected.items() if getattr(result,name)!=value]
        if mismatched:raise ProposalSnapshotError('Manager result does not match frozen binding fields: '+', '.join(mismatched)+'. Copy them from this planning request; do not invent bindings.')
        if request['status']=='staged':
            if request['proposal_json']!=encode(result.model_dump()):raise ValueError('Planner result is immutable')
            return
        if draft_revision is not None:
            draft=db.execute('SELECT * FROM workflow_plan_drafts WHERE planning_request_id=?',(request_id,)).fetchone()
            if (not draft or draft['status']!='OPEN' or draft['revision']!=draft_revision
                    or digest(json.loads(draft['draft_json']))!=draft_digest):
                raise ProposalSnapshotError('Planning draft changed before atomic proposal staging')
        if proposal and db.execute('SELECT id FROM planner_runs WHERE run_id=? AND proposal_id=? AND id<>?',
                      (run['id'],proposal.proposal_id,request_id)).fetchone():
            raise ProposalIdentityError('proposal_id is already used, including rejected proposals; use this planning_request_id as a new proposal_id')
        if proposal and proposal.base_graph_version!=request['base_graph_version']:raise ValueError('Proposal does not match frozen planning version')
        validate_references(store,run,proposal)
        db.execute("UPDATE planner_runs SET proposal_json=?,proposal_id=?,status='staged',lease_id=?,lease_generation=?,updated_at=? WHERE id=?",
                   (encode(result.model_dump()),proposal.proposal_id if proposal else None,lease.attempt,lease.generation,store.now(),request_id))
        if draft_revision is not None:
            db.execute("UPDATE workflow_plan_drafts SET status='COMMITTED',updated_at=? WHERE planning_request_id=? AND status='OPEN' AND revision=?",
                       (store.now(),request_id,draft_revision))
        db.execute('UPDATE runs SET next_reconcile_at=? WHERE id=?',(store.now(),run['id']))
        event(store,run,'proposal_staged',{'proposal_id':proposal.proposal_id if proposal else '', 'result_kind':result.kind,'task_id':lease.step_id})


def apply(store,run,request,*,confirmed_plan=False):
    """Caller owns the reconcile fence; repeat application returns the prior version."""
    if request['status']=='applied':return request['applied_graph_version']
    if request['status']!='staged':raise ValueError('Planner result has not been staged')
    if request['authorization_version']!=run['authorization_version']:raise ValueError('Planning authorization changed')
    result=ManagerResult.model_validate(json.loads(request['proposal_json']))
    if not run['scope_confirmed'] and result.kind not in ('REQUEST_AUTHORIZATION','REQUEST_USER_INPUT','NEW_RUN_REQUIRED','RESPOND'):
        raise ValueError('Business scope has not been confirmed')
    if result.base_graph_version!=run['graph_version'] or result.snapshot_digest!=request['snapshot_digest']:
        raise ValueError('Manager result is stale')
    trigger=json.loads(request['trigger_json']);frozen=json.loads(request['snapshot_json']);context=frozen.get('repair_context',{})
    if result.proposal is None:return apply_control_result(store,run,request,result,frozen)
    from .workflow_model_protocol import scopes
    repair_scope,user_scope=scopes(frozen)
    batch=frozen.get('input_batch') or {};input_records=batch.get('inputs',[])
    from .workflow_conversation import needs_confirmation,hold_plan
    if confirmed_plan and (not run['authorization']['delivery_targets'] or result.delivery_targets and set(result.delivery_targets)!=set(run['authorization']['delivery_targets'])):
        from .run_authorization import approve
        if not result.delivery_targets:raise ValueError('Confirmed plan has no delivery targets')
        if run['authorization']['delivery_targets'] and not input_records:raise ValueError('Delivery amendment lacks explicit user input')
        approve(store,run,Authorization.model_validate({**run['authorization'],'delivery_targets':result.delivery_targets}))
        run=get(store,run['id'])
    proposal,tasks,revisions,edges=validate_proposal(store,run,result.proposal.model_dump(),repair_scope=repair_scope,user_revision_scope=user_scope)
    if not confirmed_plan and needs_confirmation(run,frozen):
        hold_plan(store,run,request,result,tasks)
        return run['graph_version']

    version=run['graph_version']+1;now=store.now()
    for task in tasks:
        from .specialist_profiles import get as profile
        frozen=profile(task.profile).snapshot() if task.type=='agent' else {}
        store.execute('''INSERT INTO run_tasks(run_id,id,type,purpose,authorization_version,spec_json,profile_json,status,phase,created_graph_version,created_at,updated_at)
            VALUES (?,?,?,?,?,?,?,'blocked',?,?,?,?)''',(run['id'],task.id,task.type,task.purpose,run['authorization_version'],encode(task.model_dump()),encode(frozen),initial_phase(task),version,now,now))
        store.execute('''INSERT INTO workflow_task_revisions(run_id,task_id,revision,spec_json,created_graph_version,created_at)
            VALUES (?,?,0,?,?,?)''',(run['id'],task.id,encode(task.model_dump()),version,now))
    previous_edges={(edge['upstream_task_id'],edge['downstream_task_id']) for edge in run['dependencies']}
    for upstream,downstream in previous_edges-set(edges):
        store.execute('''UPDATE workflow_dependencies SET retired_graph_version=?
            WHERE run_id=? AND upstream_task_id=? AND downstream_task_id=? AND retired_graph_version IS NULL''',
            (version,run['id'],upstream,downstream))
    for upstream,downstream in edges:
        if (upstream,downstream) not in previous_edges:
            store.execute('INSERT INTO workflow_dependencies(run_id,upstream_task_id,downstream_task_id,created_graph_version) VALUES (?,?,?,?)',
                          (run['id'],upstream,downstream,version))
    for old,new in proposal.replacements.items():
        previous=next(task for task in run['tasks'] if task['id']==old)
        store.execute('UPDATE run_tasks SET selected=0 WHERE run_id=? AND id=?',(run['id'],old))
        store.execute('UPDATE run_tasks SET repair_count=?,user_revision_count=?,repair_context_json=? WHERE run_id=? AND id=?',
            (previous['repair_count']+int(user_scope is None),previous['user_revision_count']+int(user_scope is not None),
             encode({'automatic':context,'inputs':input_records,'replaces':old}),run['id'],new))
        store.execute('INSERT INTO run_task_replacements VALUES (?,?,?,?,?,?)',
            (run['id'],old,new,version,'user_feedback' if user_scope is not None else 'automatic_repair',now))

    if revisions:
        affected=set(revisions)
        while True:
            expanded=affected|{downstream for upstream,downstream in edges if upstream in affected}
            if expanded==affected:break
            affected=expanded
        current={task['id']:task for task in active_tasks(run)}
        # Newly inserted descendants already have revision 0. Only pre-existing
        # Tasks have a previous immutable revision to supersede.
        for identifier in sorted(affected & current.keys()):
            row=current[identifier];new_revision=row['revision']+1
            spec=revisions.get(identifier) or Task.model_validate(row['spec'])
            repair_root=identifier in {task.id for task in proposal.revise_tasks}
            store.execute('''INSERT INTO workflow_task_revisions(run_id,task_id,revision,spec_json,created_graph_version,
                repair_root,supersedes_revision,reason,created_at) VALUES (?,?,?,?,?,?,?,?,?)''',
                (run['id'],identifier,new_revision,encode(spec.model_dump()),version,int(repair_root),row['revision'],proposal.reason,now))
            store.execute('''UPDATE run_tasks SET spec_json=?,purpose=?,authorization_version=?,revision=?,repair_count=repair_count+?,user_revision_count=user_revision_count+?,repair_context_json=?,status='blocked',phase=?,
                attempts=0,recovery_count=0,infrastructure_interruptions=0,review_again_count=0,next_run_at=0,wait_reason='',candidate_result_json='[]',accepted_result_json='[]',quality_json='{}',
                bindings_json='{}',external_id='',error_code='',error_detail='',lease_id='',lease_generation=0,updated_at=?
                WHERE run_id=? AND id=?''',(encode(spec.model_dump()),spec.purpose,run['authorization_version'],new_revision,int(repair_root and user_scope is None),int(repair_root and user_scope is not None),encode({'automatic':context,'inputs':input_records}),initial_phase(spec),now,run['id'],identifier))
    repair_context='{}' if revisions or proposal.replacements or proposal.review_again else encode(run.get('repair_context',{}))
    if not store.execute('''UPDATE runs SET graph_version=?,sealed=?,replan_after_json=?,repair_context_json=?,status='ACTIVE',
        next_reconcile_at=?,updated_at=? WHERE id=? AND graph_version=? AND sealed=0''',
        (version,int(proposal.sealed),encode(proposal.replan_after),repair_context,now,now,run['id'],proposal.base_graph_version)):
        raise ValueError('Graph changed before proposal commit')
    store.execute("UPDATE planner_runs SET status='applied',applied_graph_version=?,updated_at=? WHERE id=?",(version,now,request['id']))
    store.execute("UPDATE workflow_plan_drafts SET status='COMMITTED',updated_at=? WHERE planning_request_id=? AND status='OPEN'",(now,request['id']))
    store.execute("UPDATE control_jobs SET status='succeeded',error_code='',error_detail='',wait_reason='',updated_at=? WHERE run_id=? AND id=?",(now,run['id'],request['task_id']))
    selected=store.query('SELECT id,revision FROM run_tasks WHERE run_id=? AND selected=1',(run['id'],))
    for item in selected:
        store.execute('INSERT INTO workflow_plan_tasks(run_id,graph_version,task_id,task_revision,selected_at) VALUES (?,?,?,?,?)',
                      (run['id'],version,item['id'],item['revision'],now))
    if proposal.review_again:
        from .workflow_orchestrator import Orchestrator
        controller=Orchestrator(store)
        for subject,evidence in proposal.review_again.items():
            quality=dict(next(task for task in run['tasks'] if task['id']==subject)['quality'])
            quality.pop('agent_review_passed',None)
            store.execute("UPDATE run_tasks SET status='reviewing',accepted_result_json='[]',quality_json=?,review_again_count=review_again_count+1,error_code='',error_detail='' WHERE run_id=? AND id=?",(encode(quality),run['id'],subject))
            controller.queue_review(get(store,run['id']),subject,evidence_tasks=evidence)
    event(store,{**run,'graph_version':version},'proposal_applied',{'proposal_id':proposal.proposal_id,'added':[t.id for t in tasks],
        'revised':sorted(revisions),'sealed':proposal.sealed})
    if result.input_through_sequence:
        from .run_inputs import finish_batch
        finish_batch(store,get(store,run['id']),result.input_through_sequence)
    return version


def apply_control_result(store,run,request,result,frozen):
    from .workflow_orchestrator import Orchestrator
    from .run_inputs import finish_batch,defer_batch
    now=store.now()
    if result.kind=='RESPOND':
        from .workflow_conversation import respond
        respond(store,run,request,result)
    elif result.kind=='CONTINUE':
        if not result.input_through_sequence:raise ValueError('CONTINUE requires an input batch')
        from .workflow_graph import asset_digest,AuthorizationRequired
        assets={asset['id']:asset for asset in store.assets(run['project_id'])}
        for task in active_tasks(run):
            for ref in task['spec'].get('inputs',{}).values():
                if ref.get('asset') and (ref['asset'] not in assets or run['authorization']['allowed_asset_digests'].get(ref['asset'])!=asset_digest(assets[ref['asset']])):
                    raise AuthorizationRequired('CONTINUE cannot authorize changed input content')
        store.execute("UPDATE runs SET sealed=?,status='ACTIVE',terminal_reason='' WHERE id=?",
                      (int((frozen.get('input_batch') or {}).get('was_sealed',False)),run['id']))
        finish_batch(store,run,result.input_through_sequence)
    elif (result.kind=='REQUEST_USER_INPUT' and not run['tasks'] and
          len(store.query("SELECT id FROM workflow_decisions WHERE run_id=? AND kind='input_question' LIMIT 3",(run['id'],)))>=3):
        finish_batch(store,run,result.input_through_sequence,status='FAILED')
        store.execute("UPDATE runs SET status='WAITING_USER',terminal_reason='clarification_exhausted' WHERE id=?",(run['id'],))
    else:
        request_kind={'REQUEST_AUTHORIZATION':'plan_change','REQUEST_USER_INPUT':'input_question',
                      'NEW_RUN_REQUIRED':'new_run'}[result.kind]
        if result.kind=='REQUEST_AUTHORIZATION' and not run['scope_confirmed']:request_kind='successor_authorization'
        data={'reason':result.reason,'question':result.question,'options':result.options,
              'input_through_sequence':result.input_through_sequence,'new_goal':result.new_goal}
        if result.authorization:data['authorization']=result.authorization.model_dump()
        if result.authorization and set(result.authorization.delivery_targets)!=set(run['authorization']['delivery_targets']):
            from .effective_delivery import change_details
            data.update(previous_delivery_targets=run['authorization']['delivery_targets'],delivery_scope_changed=True,
                        delivery_targets=result.authorization.delivery_targets,
                        delivery_changes=change_details(frozen,result.authorization.delivery_targets))
        Orchestrator(store).decision(run,request_kind,data)
        if result.input_through_sequence:defer_batch(store,run,result.input_through_sequence)
    store.execute("UPDATE planner_runs SET status='applied',applied_graph_version=?,updated_at=? WHERE id=?",(run['graph_version'],now,request['id']))
    store.execute("UPDATE workflow_plan_drafts SET status='COMMITTED',updated_at=? WHERE planning_request_id=? AND status='OPEN'",(now,request['id']))
    store.execute("UPDATE control_jobs SET status='succeeded',error_code='',error_detail='',wait_reason='',updated_at=? WHERE run_id=? AND id=?",(now,run['id'],request['task_id']))
    event(store,run,'manager_result_applied',{'result_kind':result.kind,'through_sequence':result.input_through_sequence})
    return run['graph_version']
