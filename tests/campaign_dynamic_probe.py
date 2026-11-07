"""Auditable live-case inspection/cleanup, restricted to the independent QA namespace."""
from __future__ import annotations
import argparse
import concurrent.futures
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import sys
import time

REPO=Path(os.environ.get('DYNAMIC_CAMPAIGN_REPO',Path(__file__).resolve().parents[1])).resolve()
sys.path.insert(0,str(REPO))
from deploy import dynamic_cluster as cluster
ROOT=REPO/'.cluster/campaigns/dynamic-plan'


def candidate_archive_disposition(state,record):
    if record.get('artifact'):return 'archive'
    if state=='candidate':return 'not_committed'
    return 'missing_required_artifact'


def save(path,value):
    path=Path(path);path.parent.mkdir(parents=True,exist_ok=True)
    temporary=path.with_suffix(path.suffix+'.tmp');temporary.write_text(json.dumps(value,ensure_ascii=False,indent=2)+'\n')
    temporary.chmod(0o600);temporary.replace(path)


def configure(directory):
    directory=Path(directory).resolve()
    if not directory.is_relative_to((ROOT/'runs').resolve()):raise ValueError('Not an owned dynamic QA attempt')
    state=json.loads((directory/'state.json').read_text())
    deployed=json.loads((cluster.ROOT/'release.json').read_text())
    if deployed.get('namespace')!=cluster.NAMESPACE:raise ValueError('Wrong deployment target')
    if deployed.get('state')!='ready' or not deployed.get('config_sha256'):raise ValueError('Deployment or frozen configuration is incomplete')
    frozen=state.get('release',{})
    if frozen and (frozen.get('sha256')!=deployed.get('sha256') or frozen.get('config_sha256')!=deployed.get('config_sha256')):
        raise ValueError('Deployment source or configuration changed during this case')
    if not re.fullmatch('[a-f0-9]{32}',state.get('project','')):raise ValueError('Invalid QA project')
    sys.path.insert(0,deployed['source']);os.environ.update(cluster.environment())
    cluster.validate_isolation(os.environ)
    from webapp.store import Store
    store=Store(cluster.data('node','a'),initialize_schema=False,timeout=5)
    database=store.query('SELECT current_database() AS name')[0]['name']
    if database!=cluster.DATABASE:raise ValueError('Refusing to use another database')
    projects=store.query('SELECT name,owner FROM projects WHERE id=?',(state['project'],))
    if projects and not projects[0]['name'].startswith('DYNAMIC-QA-'+state['campaign_attempt_id']+'-'):
        raise ValueError('Project is not owned by this QA attempt')
    return directory,state,deployed,store


def logs(store,state):
    from webapp.diagnostics import observation
    from campaign_archive import archive
    directory=Path(state['_directory'])
    result=archive(directory,store,observation)
    result['http_requests']=[]
    for request in state.get('requests',[])[:100]:
        identifier=request.get('request_id')
        if not identifier:continue
        try:
            page=observation('/logs',{'request_id':identifier,'limit':20})
            result['http_requests'].extend(page['events'])
        except Exception as exc:result.setdefault('gaps',[]).append(type(exc).__name__)
    return result


def object_check(store,row):
    key=row['object_key'];expected='distributed-agent-system/projects/'+row['project']+'/'
    result={'artifact_id':row['id'],'state':row['state'],'sha256':row['sha256'],'plaintext_bytes':row['size'],
            'prefix_isolated':key.startswith(expected),'object_key_digest':hashlib.sha256(key.encode()).hexdigest()}
    if not result['prefix_isolated']:return result
    try:
        backend=store.artifacts.backend
        info=backend.client.head_object(Bucket=backend.bucket,Key=key)
        result['exists']=True;result['stored_bytes']=info['ContentLength']
        from webapp.object_encryption import MAGIC,OVERHEAD
        result['encrypted_size_valid']=info['ContentLength']==row['size']+OVERHEAD
        response=backend.client.get_object(Bucket=backend.bucket,Key=key,Range='bytes=0-7')
        try:result['encrypted_header_valid']=response['Body'].read(8)==MAGIC
        finally:response['Body'].close()
    except Exception as exc:
        result['error_type']=type(exc).__name__
        response=getattr(exc,'response',{})
        result['exists']=False if response.get('ResponseMetadata',{}).get('HTTPStatusCode')==404 else None
    return result


def inspect(directory,final=False,*,supplement=False):
    directory,state,deployed,store=configure(directory)
    from webapp import workflow_graph as graph
    from webapp.diagnostics import diagnose,observation
    from webapp.agent_stream import StreamBus
    project=state['project'];tasks=store.query('SELECT id,status FROM runs WHERE project_id=? ORDER BY created_at',(project,))
    report={'case':state['case'],'run_id':state['run_id'],'at':time.time(),'release':deployed,
            'same_release':state['release']['sha256']==deployed['sha256'],'program_issues':[],'dependency_issues':[],
            'dimensions':{},'complete':True}
    report['collector_file_sha256']=hashlib.sha256(Path(__file__).read_bytes()).hexdigest()
    expected=state.get('helper_sha256',{}).get('campaign_dynamic_probe.py')
    if expected and expected!=report['collector_file_sha256'] and not supplement:raise ValueError('Frozen collector source changed')
    if supplement:
        if state.get('status')!='finished':raise ValueError('Supplemental inspection requires a finished attempt')
        backup=directory/('before-supplement-'+str(time.time_ns()));backup.mkdir(mode=0o700)
        for name in ('internal-final.json','candidate-archive.json','runtime-evidence.json','log-index.json','observability.json'):
            if (directory/name).exists():shutil.copyfile(directory/name,backup/name)
        report['supplemental_collection']={'original_collector_sha256':expected,'read_only_application_source':str(Path(graph.__file__).resolve().parents[1]),
            'source_sha256':__import__('tests.run_workflow_suite',fromlist=['source_digest']).source_digest(),
            'execution_was_not_repeated':True,'original_state_preserved':True,'original_reports_backup':backup.name}
    runs=[graph.get(store,row['id']) for row in store.query('SELECT id FROM runs WHERE project_id=? ORDER BY created_at',(project,))]
    report['database']={'runs':runs,'sessions':store.query('SELECT * FROM sessions WHERE project_id=?',(project,)),'agent_turns':store.query('SELECT id FROM agent_turns WHERE project=?',(project,))}
    checks=report['dimensions']['database']={'checked':True,'invariants':[]}
    def check(name,passed,detail=None):
        item={'name':name,'passed':bool(passed)}
        if detail is not None:item['detail']=detail
        checks['invariants'].append(item)
        if not passed:report['program_issues'].append(name)
    check('owned_project_exists_for_audit',bool(store.query('SELECT id FROM projects WHERE id=?',(project,))))
    check('browser_and_database_run_identity',set(state.get('run_ids',[]))=={run['id'] for run in runs})
    check('no_agent_turns_for_runs',not report['database']['agent_turns'])
    task_ids=[row['id'] for row in tasks]
    diagnostics=[];all_cleanup=[]
    for run in runs:
        wid=run['id'];taskmap={t['id']:t for t in run['tasks']}
        events=store.query('SELECT seq,kind,graph_version,payload_json,created_at FROM workflow_graph_events WHERE run_id=? ORDER BY seq',(wid,))
        applied=[e for e in events if e['kind']=='proposal_applied']
        check('graph_versions_monotonic:'+wid,[e['graph_version'] for e in applied]==list(range(1,run['graph_version']+1)))
        if run['terminal_reason']=='delivery_targets_incomplete':
            blocked=[json.loads(e['payload_json']) for e in events if e['kind']=='delivery_blocked']
            explained=bool(blocked and (blocked[-1].get('missing_task_ids') or blocked[-1].get('missing_extensions') or blocked[-1].get('missing_roles')))
            check('delivery_target_block_explained:'+wid,explained,blocked[-1] if blocked else {})
        for dependency in run['dependencies']:
            check('owned_dependency:'+dependency['downstream_task_id'],dependency['upstream_task_id'] in taskmap and dependency['downstream_task_id'] in taskmap)
        proposals=store.query("SELECT proposal_json,applied_graph_version,base_graph_version,status FROM planner_runs WHERE run_id=? AND status='applied' ORDER BY applied_graph_version",(wid,))
        expected={}
        modes={}
        for proposal in proposals:
            result=json.loads(proposal['proposal_json']);body=result.get('proposal')
            if not body:continue
            modes[proposal['applied_graph_version']]=bool(result.get('input_through_sequence'))
            for task in body['add_tasks']:
                check('task_declared_once:'+task['id'],task['id'] not in expected)
                expected[task['id']]=task
        for task in run['tasks']:
            revisions=store.query('''SELECT revision,spec_json,created_graph_version,repair_root,supersedes_revision
                FROM workflow_task_revisions WHERE run_id=? AND task_id=? ORDER BY revision''',(wid,task['id']))
            decoded=[{**row,'spec':json.loads(row['spec_json'])} for row in revisions]
            check('immutable_task_revision_history:'+task['id'],bool(decoded) and [row['revision'] for row in decoded]==list(range(task['revision']+1))
                  and decoded[0]['spec']==expected.get(task['id']) and decoded[-1]['spec']==task['spec'])
            prior=store.query('SELECT old_task_id,source FROM run_task_replacements WHERE run_id=? AND new_task_id=?',(wid,task['id']))
            inherited_auto=inherited_user=0
            if prior:
                predecessor=taskmap[prior[0]['old_task_id']]
                inherited_auto=predecessor['repair_count']+int(prior[0]['source']=='automatic_repair')
                inherited_user=predecessor['user_revision_count']+int(prior[0]['source']=='user_feedback')
            check('repair_root_count:'+task['id'],task['repair_count']==inherited_auto+sum(row['repair_root'] for row in decoded if not modes.get(row['created_graph_version'])))
            check('user_revision_count:'+task['id'],task['user_revision_count']==inherited_user+sum(row['repair_root'] for row in decoded if modes.get(row['created_graph_version'])))
            for prior,current in zip(decoded,decoded[1:]):
                check(f'revision_supersedes:{task["id"]}:{current["revision"]}',current['supersedes_revision']==prior['revision'])
            if task['status']=='succeeded':check('technical_acceptance:'+task['id'],task['quality'].get('technical_valid') and bool(task['accepted_result']))
        selected=store.query('SELECT task_id,task_revision FROM workflow_plan_tasks WHERE run_id=? AND graph_version=?',(wid,run['graph_version']))
        selection={row['task_id']:row['task_revision'] for row in selected}
        check('current_plan_revision_selection:'+wid,selection=={t['id']:t['revision'] for t in run['tasks'] if t['selected']})
        leases=store.query('SELECT slot,attempt,generation,active,expires FROM execution_leases WHERE run_id=?',(run['id'],))
        active=[l for l in leases if l['active'] and l['expires']>store.now()]
        cleanup=store.query('SELECT attempt,state,node,updated FROM execution_cleanup WHERE task_id=?',(run['id'],));all_cleanup+=cleanup
        provider=store.query('SELECT request_key,operation,state,external_id,attempt_id,generation,authorization,implementation,usage,cost FROM workflow_provider_calls WHERE workflow=?',(wid,))
        usage={}
        for row in provider:
            binding=json.loads(row['implementation']);group=binding['budget_group'];usage[group]=usage.get(group,0)+1
            check('frozen_provider_metadata:'+row['request_key'],all(key in binding for key in ('external_side_effects','budget_group','concurrency_group','route_adapter','provider_identity','cleanup_category')))
        check('provider_budget:'+wid,all(count<=run['authorization']['provider_call_budget'].get(op,0) for op,count in usage.items()),usage)
        results=store.query('SELECT task_id,task_revision,lease_id,lease_generation,status,outcome,error_json,created_at,committed_at FROM task_results WHERE run_id=?',(wid,))
        for result in results:result['error']=json.loads(result.pop('error_json'))
        for result in results:
            if result['status']!='committed':continue
            newer=store.query('SELECT id FROM execution_attempts WHERE run_id=? AND step_id=? AND generation>? AND started<?',
                              (run['id'],result['task_id'],result['lease_generation'],result['committed_at']))
            check('no_stale_result_commit:'+result['lease_id'],not newer)
        reconcile_failures=store.query('SELECT reconcile_generation,exception_type,error_code,created_at FROM workflow_reconcile_failures WHERE run_id=?',(wid,))
        check('no_reconcile_failures:'+wid,not reconcile_failures,reconcile_failures)
        if run['status']=='SUCCEEDED':
            check('sealed_before_delivery:'+wid,run['sealed'] and run['delivered_at'] is not None)
            check('all_required_tasks_accepted:'+wid,all(t['status']=='succeeded' for t in run['tasks'] if t['selected']))
            check('one_delivery:'+wid,len([e for e in events if e['kind']=='delivered'])==1)
            check('no_active_lease_at_terminal:'+wid,not active)
            check('no_unknown_provider_at_success:'+wid,all(p['state'] in ('succeeded','failed','cancelled','rejected') for p in provider))
            reviews=store.query("SELECT id,status,graph_version,decision_json,review_identity,snapshot_json FROM workflow_reviews WHERE run_id=? AND purpose='final' AND status='passed'",(wid,))
            deliveries=[json.loads(e['payload_json']) for e in events if e['kind']=='delivered']
            chosen=next((review for review in reviews if deliveries and review['id']==deliveries[0].get('review_id')),None)
            valid=bool(chosen and chosen['review_identity']==deliveries[0].get('review_identity'))
            if valid:
                from webapp.review_identity import identity
                frozen=json.loads(chosen['snapshot_json'])
                frozen['tasks']=[task for task in graph.active_tasks(run) if task['spec']['purpose']!='evidence']
                frozen['evidence_tasks']=[task for task in graph.active_tasks(run) if task['spec']['purpose']=='evidence']
                valid=identity(store,run,frozen)==chosen['review_identity']
            check('final_review_matches_content_and_criteria:'+wid,valid)
        if final and run['status'] in graph.RUN_TERMINAL:
            check('local_cleanup_complete:'+wid,all(c['state']=='complete' for c in cleanup))
        if not run['sealed']:check('open_graph_not_delivered:'+wid,run['delivered_at'] is None)
        inputs=store.query('SELECT * FROM run_inputs WHERE run_id=? ORDER BY sequence',(wid,))
        check('input_sequence_contiguous:'+wid,[item['sequence'] for item in inputs]==list(range(1,run['input_sequence']+1)))
        grants=store.query('SELECT version,authorization_json FROM run_authorizations WHERE run_id=? ORDER BY version',(wid,))
        check('authorization_ledger_contiguous:'+wid,[grant['version'] for grant in grants]==list(range(1,run['authorization_version']+1)))
        check('current_scope_matches_immutable_grant:'+wid,bool(grants) and json.loads(grants[-1]['authorization_json'])==run['authorization'])
        check('every_work_item_has_a_frozen_grant:'+wid,all(item['authorization_version'] in {grant['version'] for grant in grants} for item in graph.work_items(run)))
        planner_snapshots=store.query('SELECT id,snapshot_json,snapshot_digest,proposal_json,status FROM planner_runs WHERE run_id=? ORDER BY created_at',(wid,))
        plan_drafts=store.query('''SELECT planning_request_id,snapshot_digest,base_graph_version,authorization_version,
            input_through_sequence,schema_version,status,revision,last_error_signature,last_error_json,repeated_error_count FROM workflow_plan_drafts
            WHERE run_id=? ORDER BY created_at''',(wid,))
        for draft in plan_drafts:draft['last_error']=json.loads(draft.pop('last_error_json'))
        draft_by_id={item['planning_request_id']:item for item in plan_drafts}
        check('every_planner_has_bound_draft:'+wid,len(draft_by_id)==len(planner_snapshots) and all(
            planner['id'] in draft_by_id and draft_by_id[planner['id']]['schema_version']=='plan-draft-v1'
            for planner in planner_snapshots))
        for planner in planner_snapshots:
            frozen=json.loads(planner['snapshot_json']);batch=frozen.get('input_batch')
            draft=draft_by_id.get(planner['id'])
            check('plan_draft_binding:'+planner['id'],bool(draft) and draft['snapshot_digest']==planner['snapshot_digest']
                and draft['base_graph_version']==frozen['graph_version'] and draft['authorization_version']==frozen['authorization_version']
                and draft['input_through_sequence']==(batch or {}).get('through_sequence',0))
            if planner['status']=='applied':check('applied_plan_draft_committed:'+planner['id'],draft['status']=='COMMITTED')
            elif planner['status']=='rejected':check('rejected_plan_draft_terminal:'+planner['id'],draft['status'] in ('REJECTED','EXPIRED'))
            if not batch:continue
            cutoff=batch['through_sequence']
            check('planner_input_cutoff:'+planner['id'],bool(batch['inputs']) and max(item['sequence'] for item in batch['inputs'])==cutoff)
            if planner['status']=='applied':
                check('manager_result_matches_input_batch:'+planner['id'],json.loads(planner['proposal_json'])['input_through_sequence']==cutoff)
        attempts=store.query('''SELECT a.id,a.step_id,a.task_revision,a.started FROM execution_attempts a
            JOIN run_tasks t ON t.run_id=a.run_id AND t.id=a.step_id WHERE a.run_id=? ORDER BY a.started''',(wid,))
        start=None;intervals=[]
        for event in events:
            payload=json.loads(event['payload_json'])
            if event['kind']=='input_accepted' and start is None:start=event['created_at']
            if event['kind']=='input_batch_applied' and payload.get('next_barrier_sequence') is None and start is not None:
                intervals.append((start,event['created_at']));start=None
        if start is not None:intervals.append((start,store.now()))
        for index,(start,end) in enumerate(intervals):
            drain={(attempt['step_id'],attempt['task_revision']) for attempt in attempts if attempt['started']<start}
            forbidden=[attempt['id'] for attempt in attempts if start<=attempt['started']<end and (attempt['step_id'],attempt['task_revision']) not in drain]
            check(f'barrier_blocks_new_business:{wid}:{index}',not forbidden,forbidden)
        if run['status']=='SUCCEEDED':check('no_unapplied_inputs_at_delivery:'+wid,run['input_barrier_sequence'] is None and not any(item['status'] in ('ACCEPTED','PLANNING') for item in inputs))
        check('control_jobs_are_separate:'+wid,not ({item['id'] for item in run['tasks']}&{item['id'] for item in run['control_jobs']}))
        run['audit']={'leases':leases,'cleanup':cleanup,'provider_calls':provider,'results':results,'events':events,'inputs':inputs,'drain_set':run['drain_set'],
                      'authorizations':grants,'planner_snapshots':planner_snapshots,'plan_drafts':plan_drafts,'barrier_intervals':intervals,
                      'task_revisions':store.query('SELECT * FROM workflow_task_revisions WHERE run_id=? ORDER BY task_id,revision',(wid,)),
                      'plan_selections':store.query('SELECT * FROM workflow_plan_tasks WHERE run_id=? ORDER BY graph_version,task_id',(wid,)),
                      'step_checkpoints':store.query('SELECT step,revision,manifest,updated FROM execution_step_checkpoints WHERE workflow=? ORDER BY step,revision',(wid,))}
        diagnostics.append(diagnose(store,run['id'],limit=200,include_logs=True))
    report['diagnostics']=diagnostics
    report['execution_attempts']=store.query('''SELECT a.id,a.run_id,a.step_id,a.kind,a.generation,a.status,
        a.started,a.finished,a.exit_reason,a.uid,a.task_revision,w.node,w.release
        FROM execution_attempts a LEFT JOIN execution_workers w ON w.id=a.worker
        WHERE a.run_id IN (SELECT id FROM runs WHERE project_id=?) ORDER BY a.started''',(project,))
    check('execution_release_matches_case',all(a['release']==Path(state['release']['source']).name for a in report['execution_attempts']))
    report['review_records']=store.query('SELECT r.* FROM workflow_reviews r JOIN runs w ON w.id=r.run_id WHERE w.project_id=? ORDER BY r.created_at',(project,))
    for row in report['review_records']:
        row['snapshot']=json.loads(row.pop('snapshot_json'));row['decision']=json.loads(row.pop('decision_json'))
    report['usage']=store.query('''SELECT call_id,task_id,attempt_id,step_id,category,operation,status,
        input_tokens,output_tokens,total_tokens,receipt,error_code FROM usage_calls
        WHERE task_id IN (SELECT id FROM runs WHERE project_id=?) ORDER BY seq''',(project,))
    report['model_reservations']=store.query('''SELECT m.* FROM workflow_model_reservations m
        JOIN runs w ON w.id=m.run_id WHERE w.project_id=?''',(project,))
    if state['case']=='D13':check('budget_rejected_before_model_submission',not any(call['category']=='llm' for call in report['usage']) and not report['model_reservations'])
    report['reuse']=store.query('SELECT * FROM workflow_asset_reuse WHERE project_id=?',(project,))
    assets={asset['id']:asset for asset in store.assets(project)}
    for item in report['reuse']:
        asset=assets.get(item['asset_id'],{})
        check('immutable_reuse:'+item['asset_id'],asset.get('metadata',{}).get('provenance',{})=={
            'run_id':item['run_id'],'task_id':item['task_id'],'revision':item['revision'],'sha256':item['sha256']})
    report['inspections']=store.query('''SELECT i.* FROM workflow_inspections i
        JOIN runs w ON w.id=i.run_id WHERE w.project_id=?''',(project,))
    # Preserve bounded failure logs before authenticated project cleanup removes
    # its checkpoints. These are private QA evidence, never service telemetry.
    failure_logs=[]
    from webapp.egress_policy import scrub,secrets
    for run in runs:
        for task in run['tasks']:
            rows=store.query('SELECT revision,manifest FROM execution_step_checkpoints WHERE workflow=? AND step=? ORDER BY revision',
                             (run['id'],task['id']))
            for row in rows:
                for name,record in json.loads(row['manifest']).items():
                    if not name.endswith('.log') or not record.get('artifact'):continue
                    try:
                        location=store.artifacts.materialize(project,record['artifact'])
                        with location.open('rb') as stream:
                            stream.seek(max(0,location.stat().st_size-16384));tail=stream.read(16384).decode(errors='replace')
                        failure_logs.append({'run_id':run['id'],'task_id':task['id'],'revision':row['revision'],'path':name,
                            'sha256':record['sha256'],'tail':scrub(tail,secrets())})
                    except Exception as exc:failure_logs.append({'run_id':run['id'],'task_id':task['id'],'path':name,'error_type':type(exc).__name__})
    save(directory/'runtime-evidence.json',failure_logs)
    report['runtime_evidence']='runtime-evidence.json'
    # Candidate bytes remain necessary even when a model rejects delivery.
    # Copy and hash them before deleting the QA project and its object references.
    archive=directory/'candidates';archive.mkdir(mode=0o700,exist_ok=True)
    candidate_records=[];copied={};archive_bytes=0;archive_errors=[]
    for run in runs:
        for task in run['tasks']:
            rows=store.query('SELECT revision,manifest FROM execution_step_checkpoints WHERE workflow=? AND step=? ORDER BY revision',
                             (run['id'],task['id']))
            for checkpoint in rows:
                manifest=json.loads(checkpoint['manifest'])
                names={name for name in manifest if name.startswith(f"edit/workflows/{run['id']}/{task['id']}/") and not name.endswith('.log')}
                if checkpoint['revision']==task['revision']:names.update(task['candidate_result']);names.update(task['accepted_result'])
                for name in sorted(names):
                    record=manifest.get(name,{})
                    item={'run_id':run['id'],'task_id':task['id'],'revision':checkpoint['revision'],'logical_path':name,
                          'state':('accepted' if name in task['accepted_result'] else 'candidate') if checkpoint['revision']==task['revision'] else 'historical_checkpoint','sha256':record.get('sha256')}
                    disposition=candidate_archive_disposition(item['state'],record)
                    if disposition=='not_committed':
                        # Failed validation may preserve a model-declared path in
                        # candidate_result even though no artifact was committed.
                        # The absence is complete evidence; there are no bytes to archive.
                        item['availability']='not_committed'
                        candidate_records.append(item)
                        continue
                    if disposition=='missing_required_artifact':
                        item['error_type']='MissingArtifact';archive_errors.append(item);candidate_records.append(item)
                        continue
                    try:
                        identifier=record['artifact']
                        if identifier not in copied:
                            if archive_bytes+record['size']>1024**3:raise ValueError('Candidate archive exceeds 1 GiB budget')
                            source=store.artifacts.materialize(project,identifier)
                            target=archive/(identifier+Path(name).suffix)
                            shutil.copyfile(source,target);target.chmod(0o600)
                            with target.open('rb') as stream:actual=hashlib.file_digest(stream,'sha256').hexdigest()
                            if actual!=record['sha256']:raise ValueError('Candidate archive hash mismatch')
                            copied[identifier]=str(target.relative_to(directory));archive_bytes+=target.stat().st_size
                        item['file']=copied[identifier]
                    except Exception as exc:
                        item['error_type']=type(exc).__name__;archive_errors.append(item)
                    candidate_records.append(item)
    save(directory/'candidate-archive.json',candidate_records)
    report['dimensions']['candidate_archive']={'checked':not archive_errors,'complete':not archive_errors,
        'manifest':'candidate-archive.json','files':len(copied),'bytes':archive_bytes,'errors':archive_errors}
    if archive_errors:report['complete']=False
    report['dimensions']['local_cleanup']={'checked':True,'records':all_cleanup}
    report['dimensions']['external_cleanup']={'checked':True,'records':store.query('SELECT id,state,phase,error FROM execution_external_cleanup WHERE task_id IN (SELECT id FROM runs WHERE project_id=?)',(project,))}
    objects=store.query('SELECT id,project,object_key,sha256,size,state FROM artifact_objects WHERE project=?',(project,))
    # Initialize once before concurrent reads; Store lazily constructs this client.
    if objects:store.artifacts.backend
    with concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:
        observed=list(pool.map(lambda row:object_check(store,row),objects[:1000]))
    report['dimensions']['boss']={'checked':True,'coverage':'HEAD and encrypted header for every retained object; plaintext hashes for downloaded deliveries',
                                  'total':len(objects),'objects':observed,'complete':len(objects)<=1000}
    for item in observed:
        if not item['prefix_isolated']:report['program_issues'].append('BOSS prefix escaped')
        if item['state']=='published' and item.get('exists') is False:report['program_issues'].append('Published BOSS object missing')
        if item.get('exists') and not (item.get('encrypted_size_valid') and item.get('encrypted_header_valid')):report['program_issues'].append('BOSS encryption/integrity metadata mismatch')
        if item.get('exists') is None:report['dependency_issues'].append('BOSS availability unknown')
    bus=StreamBus();cache={'checked':True,'configured':bool(bus.url),'prefix':bus.prefix,'streams':[],'files':[]}
    if bus.url:
        client=bus._get()
        if client is None:report['dependency_issues'].append('Redis unavailable');cache['available']=False
        else:
            cache['available']=True
            for task in task_ids:
                key=bus.key(task);ttl=client.ttl(key)
                cache['streams'].append({'task_id':task,'ttl':ttl,'length':client.xlen(key) if ttl!=-2 else 0,'prefix_isolated':key.startswith('distributed-agent-system:')})
                if ttl==-1:report['program_issues'].append('Redis stream has no expiration')
    for role in ('api','node'):
        for node in cluster.PORTS:
            for part in ('projects','cache/objects'):
                folder=cluster.data(role,node)/part/project
                entries=list(folder.rglob('*')) if folder.exists() else []
                cache['files'].append({'role':role,'node':node,'group':part,'files':sum(p.is_file() and not p.is_symlink() for p in entries),
                                       'bytes':sum(p.stat().st_size for p in entries if p.is_file() and not p.is_symlink())})
    report['dimensions']['cache']=cache
    state['_directory']=str(directory)
    try:
        archived=logs(store,state);save(directory/'log-index.json',archived)
        report['dimensions']['logs']={'checked':True,'index':'log-index.json','tasks':archived['tasks'],
            'http_events':len(archived['http_requests']),'gaps':archived.get('gaps',[])}
    except Exception as exc:report['dimensions']['logs']={'checked':False,'error_type':type(exc).__name__};report['dependency_issues'].append('Log archive unavailable')
    try:
        health=observation('/health');snapshot=observation('/snapshot');alerts=observation('/alerts')
        save(directory/'observability.json',{'health':health,'snapshot':snapshot,'alerts':alerts})
        report['dimensions']['observability']={'checked':True,'evidence':'observability.json','ready':health.get('ready'),
                                             'orchestrators_available':snapshot.get('gauges',{}).get('orchestrators_available')}
    except Exception as exc:report['dimensions']['observability']={'checked':False,'error_type':type(exc).__name__};report['dependency_issues'].append('Observer unavailable')
    screenshots=[p.name for p in directory.glob('*.png')]
    report['dimensions']['browser']={'checked':bool(state.get('browser_checked')),'screenshots':screenshots,
        'page_errors':state.get('page_errors',[]),'console_errors':state.get('console_errors',[]),'assertions':state.get('browser_assertions',[])}
    if state.get('page_errors'):report['program_issues'].append('Browser JavaScript error')
    if final and not state.get('browser_checked'):report['complete']=False
    if not report['same_release']:report['complete']=False;report['dependency_issues'].append('Deployment changed during this attempt')
    report['complete'] &= all(value.get('checked',False) for value in report['dimensions'].values()) and len(objects)<=1000
    report['program_passed']=not report['program_issues']
    save(directory/('internal-supplement.json' if supplement else 'internal-final.json' if final else 'internal-snapshot.json'),report)
    return {'case':state['case'],'program_passed':report['program_passed'],'complete':report['complete'],
            'program_issues':report['program_issues'],'dependency_issues':report['dependency_issues']}


def cleanup(directory):
    directory,state,deployed,store=configure(directory);project=state['project']
    if store.query('SELECT id FROM projects WHERE id=?',(project,)):raise ValueError('Delete the owned QA project through its authenticated API first')
    task_ids=state.get('task_ids',[])
    if any(not re.fullmatch('[a-f0-9]{32}',t) for t in task_ids):raise ValueError('Invalid retained task identity')
    if any(store.query('SELECT attempt FROM execution_cleanup WHERE task_id=? AND state<>\'complete\'',(task,)) for task in task_ids):raise ValueError('Runtime cleanup still pending')
    objects=store.query('SELECT id,object_key FROM artifact_objects WHERE project=?',(project,))
    if any(not row['object_key'].startswith('distributed-agent-system/projects/'+project+'/') for row in objects):raise ValueError('BOSS object escaped case scope')
    store.execute("UPDATE artifact_objects SET expires=? WHERE project=? AND state IN ('uploading','staged','deleting')",(store.now()-1,project))
    deleted=0
    while True:
        count=store.artifacts.collect_garbage(limit=100,project=project);deleted+=count
        if not count:break
    from webapp.agent_stream import StreamBus
    bus=StreamBus();redis={'configured':bool(bus.url),'deleted':0}
    if bus.url:
        client=bus._get()
        if client is None:raise RuntimeError('Cannot verify Redis cleanup')
        if not bus.prefix.startswith('distributed-agent-system:'):raise ValueError('Unexpected cache prefix')
        if task_ids:redis['deleted']=client.delete(*(bus.key(t) for t in task_ids))
    removed=[]
    for role in ('api','node'):
        for node in cluster.PORTS:
            for part in ('projects','cache/objects'):
                target=cluster.data(role,node)/part/project
                if target.is_symlink():raise ValueError('Unexpected cache link')
                if target.exists():shutil.rmtree(target);removed.append({'role':role,'node':node,'group':part})
    backend=store.artifacts.backend;remaining=[]
    for row in objects:
        try:backend.client.head_object(Bucket=backend.bucket,Key=row['object_key']);remaining.append(row['id'])
        except Exception as exc:
            if getattr(exc,'response',{}).get('ResponseMetadata',{}).get('HTTPStatusCode')!=404:raise
    result={'complete':not remaining,'project_removed':not store.query('SELECT id FROM projects WHERE id=?',(project,)),
            'boss_objects_deleted':deleted,'boss_remaining':remaining,'redis':redis,'cache_directories_removed':removed,
            'tombstones_retained':len(store.query("SELECT id FROM artifact_objects WHERE project=? AND state='deleted'",(project,))),
            'at':time.time()}
    for name in ('browser-state.json','private-session.json'):(directory/name).unlink(missing_ok=True)
    save(directory/'cleanup.json',result);return result


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action',choices=['inspect','final','cleanup','verify'])
    parser.add_argument('directory',type=Path);args=parser.parse_args()
    if args.action=='verify':
        directory,_,_,_=configure(args.directory)
        from campaign_probe import verify
        result=verify(directory);save(directory/'artifact-verification.json',result)
    elif args.action=='cleanup':result=cleanup(args.directory)
    else:result=inspect(args.directory,args.action=='final')
    print(json.dumps(result,ensure_ascii=False))


if __name__=='__main__':main()
