"""Frozen planning requests and atomic, idempotent DAG proposal application."""
import json
import os
import uuid
from .workflow_graph import Authorization,Proposal,Task,encode,digest,get,event,validate_proposal


class ProposalIdentityError(ValueError):pass
class ProposalReferenceError(ValueError):pass


def available_operations(store):
    from .operations import catalog
    advertised={}
    for row in store.query('''SELECT c.catalogue FROM execution_operation_catalog c JOIN execution_workers w ON w.id=c.worker
        WHERE w.draining=0 AND w.heartbeat>?''',(store.now()-30,)):
        for op in json.loads(row['catalogue']).get('operations',[]):
            if op['id'] not in advertised or op.get('available'):advertised[op['id']]=op
    if not advertised and os.environ.get('VIDEO_USE_STORAGE_METADATA_ONLY')=='1':return None
    return list(advertised.values()) or catalog()['operations']


def bind_scope(store,raw):
    from .workflow_graph import bind_authorization,Authorization
    requested=Authorization.model_validate(raw)
    operations=available_operations(store)
    if not operations:raise ValueError('No worker capability catalogue is available for scope confirmation')
    bindings={operation['id']:operation['implementation'] for operation in operations}
    if any(name!='script' and name not in bindings for name in requested.allowed_operations):
        raise ValueError('An approved operation is not advertised by workers')
    return bind_authorization(requested.model_dump(),implementations=bindings)


def snapshot(store,run):
    from .specialist_profiles import catalogue as profiles
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
        "SELECT id,purpose,subject_task,graph_version,status,decision_json FROM workflow_reviews WHERE workflow_id=? AND status='rejected' AND decision_json<>'{}' ORDER BY updated_at DESC LIMIT 10",(run['id'],))]
    reservations=store.query('SELECT COALESCE(SUM(COALESCE(used_tokens,reserved_tokens)),0) AS used FROM workflow_model_reservations WHERE workflow_id=?',(run['id'],))[0]['used']
    provider_used={row['operation']:row['n'] for row in store.query('SELECT operation,COUNT(*) AS n FROM workflow_provider_calls WHERE workflow=? GROUP BY operation',(run['id'],))}
    operation_index=[{'id':op['id'],'authorized':op['id'] in run['authorization']['allowed_operations'],
        'available':op.get('available',False),'provider':op.get('provider',''),'implementation':op.get('implementation'),
        'outputs':op.get('output_contract',{}).get('roles',[]),'contract_digest':digest(op)} for op in operations]
    return {'workflow_id':run['id'],'goal':run['goal'],'graph_version':run['graph_version'],
        'sealed':run['sealed'],'authorization':run['authorization'],'authorization_version':run['authorization_version'],
        'repair_context':run.get('repair_context',{}),
        'tasks':[{k:task[k] for k in ('id','spec','status','revision','accepted_result','quality','error_code','error_detail')}
                 for task in run['tasks'] if not task['internal']],
        'dependencies':run['dependencies'],'assets':[a for a in assets if a['id'] in permitted],
        'diagnostics':diagnostics,'diagnostics_truncated':len(store.query('SELECT step FROM workflow_diagnostics WHERE workflow=? LIMIT 769',(run['id'],)))>768,
        'review_feedback':feedback,'review_feedback_truncated':len(store.query("SELECT id FROM workflow_reviews WHERE workflow_id=? AND status='rejected' LIMIT 11",(run['id'],)))>10,
        'budget_state':{'model_tokens':{'limit':run['authorization']['model_token_budget'],'used_or_reserved':reservations,
                         'remaining':max(0,run['authorization']['model_token_budget']-reservations)},
                        'planning_rounds':{'limit':run['authorization']['max_planning_rounds'],'used':len(store.query('SELECT id FROM planner_runs WHERE workflow_id=?',(run['id'],)))},
                        'provider_calls':{name:{'limit':limit,'used':provider_used.get(name,0),'remaining':max(0,limit-provider_used.get(name,0))}
                            for name,limit in run['authorization']['provider_call_budget'].items()}},
        # Discoverability is not authorization. A Manager must know the actual
        # contract to request an explicit scope change; apply() still gates it.
        'operations':operation_index,
        'profiles':[{**p,'authorized':p['id'] in run['authorization']['allowed_agent_profiles']} for p in profiles()
                    if p['id'] not in ('media_planner','delivery_reviewer')],
        'proposal_schema':Proposal.model_json_schema()}


def operation_contract(store,frozen,identifier):
    indexed=next((item for item in frozen.get('operations',[]) if item['id']==identifier),None)
    if not indexed:raise ValueError('Operation is outside the frozen capability index')
    current=next((item for item in (available_operations(store) or []) if item['id']==identifier),None)
    if not current or digest(current)!=indexed['contract_digest']:
        raise ValueError('Operation contract changed after the planning snapshot')
    return {**current,'authorized':indexed['authorized'],'contract_digest':indexed['contract_digest']}


def queue(store,run,trigger):
    """Called in the orchestrator's fenced transaction."""
    current=store.query("SELECT id FROM planner_runs WHERE workflow_id=? AND status IN ('pending','running','staged')",(run['id'],))
    if current:return current[0]['id']
    count=store.query('SELECT COUNT(*) AS n FROM planner_runs WHERE workflow_id=?',(run['id'],))[0]['n']
    if count>=run['authorization']['max_planning_rounds']:raise ValueError('Planning round budget exhausted')
    identifier=uuid.uuid4().hex;task_id='__manager_'+identifier[:16];now=store.now()
    frozen=snapshot(store,run)
    if frozen is None:
        store.execute("UPDATE workflow_runs SET status='planning',terminal_reason='workers_unavailable' WHERE id=?",(run['id'],))
        return None
    frozen['trigger']=trigger
    frozen['planning_request_id']=identifier
    frozen['used_proposal_ids']=[row['proposal_id'] for row in store.query('SELECT proposal_id FROM planner_runs WHERE workflow_id=? AND proposal_id IS NOT NULL',(run['id'],))]
    spec={'profile':'media_planner','objective':run['goal'],'request_id':identifier,'snapshot_digest':digest(frozen)}
    store.execute('''INSERT INTO planner_runs(id,workflow_id,task_id,base_graph_version,authorization_version,
        trigger_json,snapshot_json,snapshot_digest,status,created_at,updated_at) VALUES (?,?,?,?,?,?,?,?,'pending',?,?)''',
        (identifier,run['id'],task_id,run['graph_version'],run['authorization_version'],encode(trigger),encode(frozen),digest(frozen),now,now))
    from .specialist_profiles import get as profile
    store.execute('''INSERT INTO workflow_tasks(workflow_id,id,type,spec_json,profile_json,status,internal,created_graph_version,created_at,updated_at)
        VALUES (?,?,'agent',?,?,'ready',1,?,?,?)''',(run['id'],task_id,encode(spec),encode(profile('media_planner').snapshot()),run['graph_version'],now,now))
    store.execute("UPDATE workflow_runs SET status='planning',updated_at=? WHERE id=?",(now,run['id']))
    event(store,run,'planning_requested',{'request_id':identifier,'round':count+1})
    return identifier


def stage(store,lease,request_id,raw):
    proposal=Proposal.model_validate(raw)
    with lease.bind(),store.connect() as db:
        lease.validate(db)
        request=db.execute('SELECT * FROM planner_runs WHERE id=?',(request_id,)).fetchone()
        if not request or request['task_id']!=lease.step_id:raise ValueError('Planner request outside execution scope')
        run=get(store,request['workflow_id'])
        if run['turn_id']!=lease.turn_id or request['status'] not in ('pending','running','staged'):raise ValueError('Planner request is no longer active')
        if request['status']=='staged':
            if request['proposal_json']!=encode(proposal.model_dump()):raise ValueError('Planner result is immutable')
            return
        if db.execute('SELECT id FROM planner_runs WHERE workflow_id=? AND proposal_id=? AND id<>?',
                      (run['id'],proposal.proposal_id,request_id)).fetchone():
            raise ProposalIdentityError('proposal_id is already used, including rejected proposals; use this planning_request_id as a new proposal_id')
        if proposal.base_graph_version!=request['base_graph_version']:raise ValueError('Proposal does not match frozen planning version')
        known={asset['id'] for asset in store.assets(run['project_id'])}
        if any(ref.asset and ref.asset not in known for task in proposal.add_tasks for ref in task.inputs.values()):
            raise ProposalReferenceError('Unknown input asset in this project; copy an existing asset ID exactly. Authorization cannot create a missing asset.')
        db.execute("UPDATE planner_runs SET proposal_json=?,proposal_id=?,status='staged',lease_id=?,lease_generation=?,updated_at=? WHERE id=?",
                   (encode(proposal.model_dump()),proposal.proposal_id,lease.attempt,lease.generation,store.now(),request_id))
        db.execute('UPDATE workflow_runs SET next_reconcile_at=? WHERE id=?',(store.now(),run['id']))
        event(store,run,'proposal_staged',{'proposal_id':proposal.proposal_id,'task_id':lease.step_id})


def apply(store,run,request):
    """Caller owns the reconcile fence; repeat application returns the prior version."""
    if request['status']=='applied':return request['applied_graph_version']
    if request['status']!='staged':raise ValueError('Planner result has not been staged')
    if request['authorization_version']!=run['authorization_version']:raise ValueError('Planning authorization changed')
    trigger=json.loads(request['trigger_json']);frozen=json.loads(request['snapshot_json']);context=frozen.get('repair_context',{})
    repair_scope=(context.get('subject_task','') if context.get('active') else
                  trigger.get('task_id') if 'task_id' in trigger else '' if any(key in trigger for key in ('review_rejected','task_review_rejected')) else None)
    proposal,tasks,revisions,edges=validate_proposal(store,run,json.loads(request['proposal_json']),repair_scope=repair_scope)
    version=run['graph_version']+1;now=store.now()
    for task in tasks:
        from .specialist_profiles import get as profile
        frozen=profile(task.profile).snapshot() if task.type=='agent' else {}
        store.execute('''INSERT INTO workflow_tasks(workflow_id,id,type,spec_json,profile_json,status,created_graph_version,created_at,updated_at)
            VALUES (?,?,?,?,?,'blocked',?,?,?)''',(run['id'],task.id,task.type,encode(task.model_dump()),encode(frozen),version,now,now))
        store.execute('''INSERT INTO workflow_task_revisions(workflow_id,task_id,revision,spec_json,created_graph_version,created_at)
            VALUES (?,?,0,?,?,?)''',(run['id'],task.id,encode(task.model_dump()),version,now))
    for upstream,downstream in edges:
        store.execute('INSERT OR IGNORE INTO workflow_dependencies VALUES (?,?,?,?)',(run['id'],upstream,downstream,version))
    if revisions:
        affected=set(revisions)
        while True:
            expanded=affected|{downstream for upstream,downstream in edges if upstream in affected}
            if expanded==affected:break
            affected=expanded
        current={task['id']:task for task in run['tasks'] if not task['internal']}
        for identifier in sorted(affected):
            row=current[identifier];new_revision=row['revision']+1
            spec=revisions.get(identifier) or Task.model_validate(row['spec'])
            repair_root=identifier in revisions
            store.execute('''INSERT INTO workflow_task_revisions(workflow_id,task_id,revision,spec_json,created_graph_version,
                repair_root,supersedes_revision,reason,created_at) VALUES (?,?,?,?,?,?,?,?,?)''',
                (run['id'],identifier,new_revision,encode(spec.model_dump()),version,int(repair_root),row['revision'],proposal.reason,now))
            store.execute('''UPDATE workflow_tasks SET spec_json=?,revision=?,repair_count=repair_count+?,status='blocked',phase='execute',
                attempts=0,next_run_at=0,wait_reason='',candidate_result_json='[]',accepted_result_json='[]',quality_json='{}',
                bindings_json='{}',external_id='',error_code='',error_detail='',lease_id='',lease_generation=0,updated_at=?
                WHERE workflow_id=? AND id=?''',(encode(spec.model_dump()),new_revision,int(repair_root),now,run['id'],identifier))
    repair_context='{}' if revisions else encode(run.get('repair_context',{}))
    if not store.execute('''UPDATE workflow_runs SET graph_version=?,sealed=?,replan_after_json=?,repair_context_json=?,status='running',
        next_reconcile_at=?,updated_at=? WHERE id=? AND graph_version=? AND sealed=0''',
        (version,int(proposal.sealed),encode(proposal.replan_after),repair_context,now,now,run['id'],proposal.base_graph_version)):
        raise ValueError('Graph changed before proposal commit')
    store.execute("UPDATE planner_runs SET status='applied',applied_graph_version=?,updated_at=? WHERE id=?",(version,now,request['id']))
    store.execute("UPDATE workflow_tasks SET status='succeeded',updated_at=? WHERE workflow_id=? AND id=?",(now,run['id'],request['task_id']))
    selected=store.query('SELECT id,revision FROM workflow_tasks WHERE workflow_id=? AND internal=0',(run['id'],))
    for item in selected:
        store.execute('INSERT INTO workflow_plan_tasks(workflow_id,graph_version,task_id,task_revision,selected_at) VALUES (?,?,?,?,?)',
                      (run['id'],version,item['id'],item['revision'],now))
    event(store,{**run,'graph_version':version},'proposal_applied',{'proposal_id':proposal.proposal_id,'added':[t.id for t in tasks],
        'revised':sorted(revisions),'sealed':proposal.sealed})
    return version
