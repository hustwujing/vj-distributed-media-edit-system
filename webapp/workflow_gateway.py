"""Lease-scoped proposal and untrusted Agent-result submission for harness adapters."""
import json
from . import workflow_graph as graph,workflow_planning as planning
from .specialist_profiles import get as profile

MODEL_PATHS={'/responses','/v1/responses','/messages','/v1/messages','/chat/completions','/v1/chat/completions'}


def context(lease):
    if not lease or lease.kind!='workflow' or not lease.step_id:raise ValueError('A delegated Agent task lease is required')
    lease.check();run=graph.get(lease.store,lease.run_id)
    if not run or run['status'] in ('CANCELLING',*graph.RUN_TERMINAL):raise ValueError('Workflow no longer accepts Agent submissions')
    task=next((t for t in graph.work_items(run) if t['id']==lease.step_id and t['type']=='agent'),None)
    if not task:raise ValueError('Agent task outside lease scope')
    if task.get('control_kind'):policy=profile(task['spec']['profile'])
    else:
        from .specialist_profiles import for_run_task
        policy=for_run_task(lease.store,run['id'],task['spec'])
    if graph.digest(policy.snapshot())!=graph.digest(task['profile']):raise ValueError('Agent policy changed')
    attempt=lease.store.query('SELECT started FROM execution_attempts WHERE id=?',(lease.attempt,))[0]
    seconds=min(policy.timeout_seconds,task['spec'].get('timeout_seconds',policy.timeout_seconds))
    if lease.store.now()>attempt['started']+seconds:
        from .task_leases import TaskQuotaReached
        raise TaskQuotaReached('专业 Agent 本轮运行时限已到。',category='agent_seconds',scope='turn',used=seconds,limit=seconds)
    return run,task,policy


def allowed(lease):
    run,task,policy=context(lease)
    paths=set(MODEL_PATHS)
    if 'proposal' in policy.tools:paths.add('/workflow/proposal')
    if 'result' in policy.tools:paths.add('/workflow/agent-result')
    if 'inspect' in policy.tools:paths.add('/workflow/inspect')
    from .public_http_gateway import urls_for_lease,ROUTE
    if urls_for_lease(lease):paths.add(ROUTE)
    return paths


def submit(lease,kind,body):
    run,task,policy=context(lease)
    with lease.bind(),lease.store.connect() as db:
        lease.validate(db)
        if kind=='proposal':
            if policy.id!='media_planner' or set(body)!={'proposal'}:raise ValueError('Proposal outside Manager scope')
            candidate=graph.ManagerResult.model_validate(body['proposal'])
            if candidate.proposal and any(task.type=='script' for task in (*candidate.proposal.add_tasks,*candidate.proposal.revise_tasks)):
                raise ValueError('Manager raw Script Tasks are disabled; use a registered Operation or editing_implementer')
            planning.stage(lease.store,lease,task['spec']['request_id'],body['proposal'])
            return {'staged':True,'run_id':run['id'],'base_graph_version':run['graph_version']}
        if kind!='result' or 'result' not in policy.tools or set(body)!={'result'}:raise ValueError('Result outside Agent scope')
        result=body['result']
        if not isinstance(result,dict):raise ValueError('Agent result must be an object')
        if task.get('control_kind'):
            from .workflow_agent_contracts import ReviewDecision,Diagnosis
            if policy.id=='media_failure_diagnostician':
                if set(result)!={'snapshot_digest','diagnosis'}:raise ValueError('Diagnostic result must be structured')
                Diagnosis.model_validate(result['diagnosis']);snapshot=result['snapshot_digest']
            else:
                if set(result)!={'decision'}:raise ValueError('Reviewer must submit a structured decision')
                snapshot=ReviewDecision.model_validate(result['decision']).snapshot_digest
            if snapshot!=task['spec']['snapshot_digest']:raise ValueError('Review snapshot changed')
        else:
            from .workflow_agents import coerce_result_files,validate_result_files
            files=coerce_result_files(task,result)
            validate_result_files(lease.store,lease,task,files)
            result={'files':files}
            if not set(files)<=set(task['spec']['output_roles'].values()):
                raise ValueError('Agent outputs do not match the declared roles')
            if any(not isinstance(text,str) or len(text.encode())>256000 for text in files.values()):raise ValueError('Agent output exceeds size limit')
        encoded=graph.encode(result)
        previous=db.execute('SELECT payload_json FROM workflow_agent_outputs WHERE lease_id=?',(lease.attempt,)).fetchone()
        if previous and previous['payload_json']!=encoded:raise ValueError('Agent result already staged with different content')
        db.execute('INSERT OR IGNORE INTO workflow_agent_outputs VALUES (?,?,?,?,?,?)',
                   (lease.attempt,run['id'],task['id'],lease.generation,encoded,lease.store.now()))
        return {'staged':True,'validation_required':True,'run_id':run['id'],'task_id':task['id']}


def inspect(gateway,body):
    run,task,policy=context(gateway.lease)
    if 'inspect' not in policy.tools or set(body)-{'ref','offset'}:raise ValueError('Inspection outside profile scope')
    from .workflow_agents import inputs,inspect as inspect_file,inspect_media,inspect_document
    if task.get('control_kind'):
        table='planner_runs' if policy.id=='media_planner' else 'workflow_reviews'
        row=gateway.lease.store.query(f'SELECT snapshot_json,snapshot_digest FROM {table} WHERE id=?',(task['spec']['request_id'],))[0]
        frozen=json.loads(row['snapshot_json']);snapshot_digest=row['snapshot_digest']
    else:frozen={};snapshot_digest=''
    available=inputs(gateway.lease.store,run,task,frozen)
    value=inspect_file(gateway.lease.store,run,available,body.get('ref'),body.get('offset',0))
    if value.get('media'):
        from types import SimpleNamespace
        from .workflow_runtime import Adapter
        import threading
        host=SimpleNamespace(store=gateway.lease.store,stop=threading.Event(),workflow_runner=SimpleNamespace(
            adapter=Adapter(gateway.lease.store,gateway.model,gateway.socket_path.parent)))
        value=inspect_media(host,gateway.lease,run,task,value)
    elif value.get('document'):value=inspect_document(value)
    import uuid
    gateway.lease.store.execute('INSERT INTO workflow_inspections VALUES (?,?,?,?,?,?,?,?)',
        (uuid.uuid4().hex,run['id'],task['id'],gateway.lease.attempt,body['ref'],snapshot_digest,
         graph.encode({k:v for k,v in value.items() if k not in ('text','probe','image')}),gateway.lease.store.now()))
    return value
