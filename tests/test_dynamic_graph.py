"""State protocol tests; no model or paid provider calls."""
import json
import unittest
from tests import test_workflows as fixtures
from webapp import workflow_graph as graph,workflow_planning as planning
from webapp.workflow_orchestrator import Orchestrator,ReconcileLost


def script(identifier='first',**overrides):
    return {'id':identifier,'type':'script','name':identifier,'objective':'Write the approved report',
            'argv':['python','-c','print("report")'],'outputs':['report.txt'],
            'output_roles':{'report':'report.txt'},'acceptance':[{'kind':'file'}],
            'basic_reason':'Local deterministic report','retry_policy':'safe_once','delivery_stage':True,**overrides}


def envelope(store,request_id,proposal=None,kind='APPLY_PLAN',**extra):
    request=store.query('SELECT * FROM planner_runs WHERE id=?',(request_id,))[0]
    frozen=json.loads(request['snapshot_json'])
    result={'kind':kind,'snapshot_digest':request['snapshot_digest'],
            'base_graph_version':proposal.get('base_graph_version',request['base_graph_version']) if proposal else request['base_graph_version'],
            'authorization_version':request['authorization_version'],
            'input_through_sequence':(frozen.get('input_batch') or {}).get('through_sequence',0),
            'reason':'Fixture control result',**extra}
    if proposal is not None:result['proposal']=proposal
    return result


class DynamicGraphTests(unittest.TestCase):
    def test_model_token_budget_supports_doubled_campaign_limit(self):
        authorization=graph.Authorization(goal='bounded',allowed_assets=[],allowed_operations=[],
            delivery_targets=['result'],content_fingerprint='a'*64,model_token_budget=4000000)
        self.assertEqual(authorization.model_token_budget,4000000)
        with self.assertRaises(ValueError):
            graph.Authorization.model_validate({**authorization.model_dump(),'model_token_budget':4000001})

    def setUp(self):
        self.f=fixtures.WorkflowTests();self.f.setUp();self.store=self.f.store
        self.authorization={'goal':'Make a report','allowed_assets':['asset'],'allowed_operations':['script'],
            'allowed_agent_profiles':['media_failure_diagnostician'],'delivery_targets':['report'],
            'content_fingerprint':graph.content_fingerprint(self.store,self.f.project)}
        self.run=graph.create(self.store,self.f.project,self.f.turn,self.authorization)
        self.orchestrator=Orchestrator(self.store,'test-orchestrator')
    def tearDown(self):self.f.tearDown()
    def tick(self):
        self.store.execute('UPDATE runs SET next_reconcile_at=0 WHERE id=?',(self.run['id'],))
        claims=self.orchestrator.claim_due()
        self.assertEqual(len(claims),1)
        self.orchestrator.reconcile(*claims[0])
        return graph.get(self.store,self.run['id'])
    def propose(self,raw):
        request=self.store.query("SELECT * FROM planner_runs WHERE run_id=? AND status='pending'",(self.run['id'],))[0]
        from webapp.task_leases import Coordinator
        coordinator=Coordinator(self.store,'planner-test','test')
        lease=coordinator.claim(self.f.turn,'workflow',step_id=request['task_id'])
        self.assertIsNotNone(lease)
        try:
            with lease.bind():planning.stage(self.store,lease,request['id'],envelope(self.store,request['id'],raw))
        finally:lease.release()
        return request['id']

    def test_creation_is_idempotent_and_requires_content_identity(self):
        self.assertEqual(graph.create(self.store,self.f.project,self.f.turn,self.authorization)['id'],self.run['id'])
        with self.assertRaises(ValueError):graph.create(self.store,self.f.project,self.f.turn,{**self.authorization,'goal':'different'})
        with self.assertRaises(ValueError):graph.create(self.store,self.f.project,self.f.turn,{**self.authorization,'content_fingerprint':'0'*64})

    def test_planning_snapshot_preserves_operation_output_alternatives(self):
        frozen=planning.snapshot(self.store,self.run)
        edit=next(item for item in frozen['operations'] if item['id']=='media.edit')
        self.assertEqual(edit['output_contract']['alternatives'],['audio','video'])

    def test_frozen_manager_schema_exposes_real_acceptance_target_domain(self):
        from pydantic import ValidationError
        from webapp.quality_policy import Acceptance
        self.tick()
        frozen=json.loads(self.store.query('SELECT snapshot_json FROM planner_runs WHERE run_id=?',(self.run['id'],))[0]['snapshot_json'])
        target=frozen['proposal_schema']['$defs']['Acceptance']['properties']['target']
        values=next(item['enum'] for item in target['anyOf'] if 'enum' in item)
        self.assertEqual(set(values),{'file','image','media','text','json','subtitle'})
        for value in values:
            with self.subTest(target=value):
                extra={'contains':['approved text']} if value=='text' else {}
                self.assertEqual(Acceptance(target=value,**extra).kind,value)
                self.assertEqual(Acceptance(target=value,reviewer='agent',criterion='Inspect this output').kind,'agent')
                self.assertEqual(Acceptance(target=value,reviewer='user',criterion='Confirm this output').kind,'review')
        for invalid in ('final.png','primary_image','audio','video','unknown'):
            with self.subTest(invalid=invalid),self.assertRaises(ValidationError) as error:
                Acceptance(target=invalid)
            failure=error.exception.errors(include_input=False)[0]
            self.assertEqual(failure['loc'],('target',))
            self.assertEqual(failure['type'],'literal_error')
            self.assertIn('media',failure['msg'])
        with self.assertRaises(ValidationError):Acceptance(kind='image',target='media')

    def test_script_requires_executable_not_flags_for_an_imaginary_tool(self):
        with self.assertRaisesRegex(ValueError,'executable'):
            graph.Task.model_validate(script(argv=['--input-role','source','--output','report.txt']))
        self.assertEqual(graph.Task.model_validate(script()).argv[0],'python')

    def test_specialist_report_contract_is_visible_and_cannot_require_impossible_fields(self):
        from webapp.specialist_profiles import catalogue
        profile=next(p for p in catalogue() if p['id']=='visual_quality_reviewer')
        self.assertIn('verdict',profile['result_schema']['properties'])
        task=script(type='agent',profile='visual_quality_reviewer',argv=[],outputs=['report.json'],
                    output_roles={'report':'report.json'},acceptance=[{'kind':'json','required_keys':['verdict']}],delivery_stage=False)
        graph.Task.model_validate(task)
        with self.assertRaisesRegex(ValueError,'cannot be delivery stages'):
            graph.Task.model_validate({**task,'delivery_stage':True})
        with self.assertRaisesRegex(ValueError,'fixed Agent result schema'):
            graph.Task.model_validate({**task,'acceptance':[{'kind':'json','required_keys':['verified_lines']}]})
        with self.assertRaisesRegex(ValueError,'JSON reports only'):
            graph.Task.model_validate({**task,'outputs':['report.txt'],'output_roles':{'report':'report.txt'}})

    def test_agent_acceptance_cannot_reference_an_undeclared_output(self):
        task=script(type='agent',profile='editing_implementer',argv=[],outputs=['final.png'],
                    output_roles={'primary_image':'final.png'},acceptance=[{'kind':'image','file':'final.jpg'}])
        with self.assertRaisesRegex(ValueError,'declared output'):graph.Task.model_validate(task)

    def test_rolling_plan_waits_for_trigger_and_never_delivers_unsealed(self):
        run=self.tick();self.assertEqual(run['status'],'ACTIVE')
        self.assertEqual(len(self.store.query('SELECT * FROM planner_runs')),1)
        self.tick();self.assertEqual(len(self.store.query('SELECT * FROM planner_runs')),1)
        request=self.propose({'proposal_id':'round-one','base_graph_version':0,'reason':'First inspect','add_tasks':[script()],
                             'replan_after':['first']})
        run=self.tick();self.assertEqual(run['graph_version'],1)
        task=next(t for t in graph.work_items(run) if t['id']=='first');self.assertEqual(task['status'],'ready')
        self.tick();self.assertEqual(len(self.store.query('SELECT * FROM planner_runs')),1)
        self.store.execute("UPDATE run_tasks SET status='succeeded' WHERE run_id=? AND id='first'",(run['id'],))
        run=self.tick();self.assertFalse(run['sealed']);self.assertIsNone(run['delivered_at'])
        self.assertEqual(len(self.store.query('SELECT * FROM planner_runs')),2)
        applied=self.store.query('SELECT * FROM planner_runs WHERE id=?',(request,))[0]
        with self.store.connect():self.assertEqual(planning.apply(self.store,run,applied),1)
        self.assertEqual(graph.get(self.store,run['id'])['graph_version'],1)
        self.propose({'proposal_id':'round-two','base_graph_version':1,'reason':'Append final processing',
                      'add_tasks':[script('second')],'add_dependencies':[{'upstream':'first','downstream':'second'}],'sealed':True})
        run=self.tick();self.assertEqual(run['graph_version'],2);self.assertTrue(run['sealed'])
        self.assertIsNone(run['delivered_at'])

    def test_invalid_graph_has_no_partial_task_inserts(self):
        self.tick()
        request=self.propose({'proposal_id':'cycle','base_graph_version':0,'reason':'bad cycle','add_tasks':[script('a'),script('b')],
            'add_dependencies':[{'upstream':'a','downstream':'b'},{'upstream':'b','downstream':'a'}],'sealed':True})
        self.store.execute("UPDATE workflow_plan_drafts SET status='COMMITTED' WHERE planning_request_id=?",(request,))
        run=self.tick()
        self.assertEqual(run['graph_version'],0)
        self.assertFalse([t for t in graph.work_items(run) if not t.get('control_kind')])
        self.assertEqual(self.store.query("SELECT error_code FROM planner_runs WHERE proposal_id='cycle'")[0]['error_code'],'invalid_proposal')
        self.assertEqual(self.store.query('SELECT status FROM workflow_plan_drafts WHERE planning_request_id=?',(request,))[0]['status'],'REJECTED')

    def test_scope_expansion_requires_user_decision_and_cannot_be_resumed(self):
        self.store.execute('UPDATE runs SET authorization_json=? WHERE id=?',
            (graph.encode({**self.run['authorization'],'allowed_operations':[]}),self.run['id']))
        self.tick();self.propose({'proposal_id':'unauthorized','base_graph_version':0,'reason':'Request script',
                                'add_tasks':[script()],'sealed':True})
        run=self.tick();self.assertEqual(run['status'],'WAITING_USER')
        self.assertEqual(run['graph_version'],0)
        graph.command(self.store,run['id'],'owner','resume')
        run=self.tick();self.assertEqual(run['status'],'WAITING_USER')
        self.assertEqual(self.store.query('SELECT status FROM workflow_commands')[0]['status'],'rejected')

    def test_missing_asset_is_invalid_but_existing_unapproved_asset_requires_authorization(self):
        self.tick();run=graph.get(self.store,self.run['id'])
        raw={'proposal_id':'asset-reference','base_graph_version':0,'reason':'Use source',
             'add_tasks':[script(inputs={'source':{'asset':'not-in-this-project'}})],'sealed':True}
        with self.assertRaisesRegex(ValueError,'does not exist'):
            graph.validate_proposal(self.store,run,raw)
        self.assertFalse(self.store.query('SELECT id FROM workflow_decisions'))
        self.store.add_asset(self.f.project,'unapproved','other.png','other.png',{'kind':'image','width':8,'height':8})
        raw['add_tasks'][0]['inputs']['source']['asset']='unapproved'
        self.propose(raw);current=self.tick()
        self.assertEqual(current['status'],'WAITING_USER')
        self.assertEqual(current['graph_version'],0)

    def test_pre_submit_failure_requires_changed_plan_and_never_claims_unknown_charge(self):
        self.tick();self.propose({'proposal_id':'pre-submit','base_graph_version':0,'reason':'Run',
            'add_tasks':[script()],'sealed':True});run=self.tick()
        task=next(t for t in graph.work_items(run) if t['id']=='first')
        self.orchestrator.task_failure(run,task,{'error_code':'pre_submit','phase':'execute','classification':'pre_submit'})
        current=graph.get(self.store,run['id']);task=next(t for t in graph.work_items(current) if t['id']=='first')
        self.assertEqual(task['status'],'paused');self.assertEqual(task['error_code'],'pre_submit')
        self.assertIn('尚未发起外部请求',task['error_detail'])
        graph.command(self.store,run['id'],'owner','resume');self.tick()
        self.assertEqual(self.store.query('SELECT error_code FROM workflow_commands')[0]['error_code'],'replan_required')
        self.assertFalse(self.store.query('SELECT request_key FROM workflow_provider_calls'))

    def test_planner_can_discover_unapproved_contract_without_obtaining_authority(self):
        frozen=planning.snapshot(self.store,self.run)
        operation=next(op for op in frozen['operations'] if op['id']=='image.generate_from_text')
        self.assertFalse(operation['authorized'])
        self.assertNotIn('parameters_schema',operation)
        detail=planning.operation_contract(self.store,frozen,'image.generate_from_text')
        self.assertIn('parameters_schema',detail)
        self.assertEqual(detail['contract_digest'],operation['contract_digest'])
        self.assertNotIn('image.generate_from_text',frozen['authorization']['allowed_operations'])
        self.assertNotIn('media_planner',[p['id'] for p in frozen['profiles']])

    def test_reconcile_lease_fences_expired_owner(self):
        first=self.orchestrator.claim_due()[0]
        self.store.execute('UPDATE runs SET reconcile_until=0 WHERE id=?',(self.run['id'],))
        other=Orchestrator(self.store,'other');second=other.claim_due()[0]
        with self.assertRaises(ReconcileLost):self.orchestrator.reconcile(*first)
        other.reconcile(*second)
        self.assertEqual(len(self.store.query('SELECT * FROM planner_runs')),1)

    def test_cancel_command_is_idempotent_and_blocks_planning(self):
        first=graph.command(self.store,self.run['id'],'owner','cancel',request_id='same-command')
        self.assertEqual(graph.command(self.store,self.run['id'],'owner','cancel',request_id='same-command')['id'],first['id'])
        with self.assertRaises(ValueError):graph.command(self.store,self.run['id'],'owner','pause',request_id='same-command')
        run=self.tick();self.assertEqual(run['status'],'CANCELLED')
        self.assertFalse(self.store.query('SELECT * FROM planner_runs'))
        rejected=graph.command(self.store,self.run['id'],'owner','resume')
        self.assertEqual(rejected['status'],'rejected')
        self.assertEqual(rejected['error_code'],'workflow_terminal')

    def test_pause_cannot_undo_an_earlier_cancel_command(self):
        graph.command(self.store,self.run['id'],'owner','cancel')
        graph.command(self.store,self.run['id'],'owner','pause')
        run=self.tick();self.assertEqual(run['status'],'CANCELLED')
        row=self.store.query("SELECT status,error_code FROM workflow_commands WHERE action='pause'")[0]
        self.assertEqual(row['status'],'rejected');self.assertEqual(row['error_code'],'cancellation_in_progress')

    def test_existing_tasks_cannot_gain_dependencies_or_change_contracts(self):
        self.tick();self.propose({'proposal_id':'initial','base_graph_version':0,'reason':'Initial',
                                'add_tasks':[script()],'replan_after':['first']})
        run=self.tick()
        for change in ({'add_tasks':[script()]},
                       {'add_tasks':[script('later')],'add_dependencies':[{'upstream':'later','downstream':'first'}]}):
            with self.subTest(change=change),self.assertRaises(ValueError):
                graph.validate_proposal(self.store,run,{'proposal_id':'illegal','base_graph_version':1,'reason':'Invalid','sealed':True,**change})

    def test_successful_user_revision_is_a_new_replan_trigger(self):
        self.tick();self.propose({'proposal_id':'initial-revision','base_graph_version':0,'reason':'Initial',
                                'add_tasks':[script()],'replan_after':['first']})
        run=self.tick();self.store.execute("UPDATE run_tasks SET status='succeeded',lease_id='' WHERE run_id=? AND id='first'",(run['id'],))
        run=graph.get(self.store,run['id']);current=next(task for task in graph.work_items(run) if task['id']=='first')['spec']
        revised={**current,'argv':['python','-c','print("revised")']}
        proposal,_,revisions,_=graph.validate_proposal(self.store,run,{
            'proposal_id':'user-revision','base_graph_version':run['graph_version'],'reason':'Apply user revision',
            'revise_tasks':[revised],'replan_after':['first']},user_revision_scope={'first'})
        self.assertFalse(proposal.sealed)
        self.assertEqual(set(revisions),{'first'})

    def test_scheduler_cannot_replay_before_orchestrator_accepts_a_result(self):
        from webapp.task_leases import Coordinator
        from webapp.workflow_dispatch import stage_result
        self.tick();self.propose({'proposal_id':'one','base_graph_version':0,'reason':'run once','add_tasks':[script()],'sealed':True})
        self.tick();coordinator=Coordinator(self.store,'execution-test','test')
        lease=coordinator.claim(self.f.turn,'workflow',step_id='first');self.assertIsNotNone(lease)
        try:
            with lease.bind():stage_result(self.store,lease,'failed',{'error_code':'execution_failed','phase':'execute'})
        finally:lease.release()
        self.assertIsNone(coordinator.claim(self.f.turn,'workflow',step_id='first'))
        self.assertFalse([r for r in coordinator.candidates() if r.get('step_id')=='first'])
        self.assertEqual(self.store.query("SELECT COUNT(*) AS n FROM execution_attempts WHERE step_id='first'")[0]['n'],1)

    def test_revised_repair_can_run_while_another_business_task_is_paused(self):
        from webapp.workflow_dispatch import candidates,available
        self.tick();self.propose({'proposal_id':'parallel-repair','base_graph_version':0,'reason':'Two independent tasks',
            'add_tasks':[script('failed'),script('revised')],'sealed':False,'replan_after':['failed','revised']})
        run=self.tick()
        self.store.execute("UPDATE run_tasks SET status='paused',error_code='agent_failed' WHERE run_id=? AND id='failed'",(run['id'],))
        self.store.execute("UPDATE run_tasks SET status='ready',repair_count=1 WHERE run_id=? AND id='revised'",(run['id'],))
        self.store.execute("UPDATE runs SET status='ACTIVE' WHERE id=?",(run['id'],))
        run=graph.get(self.store,run['id'])
        self.assertIn('revised',available(self.store,run,self.store.now()))
        self.assertIn('revised',{row['step_id'] for row in candidates(self.store)})
        self.store.execute("UPDATE run_tasks SET repair_count=0 WHERE run_id=? AND id='revised'",(run['id'],))
        run=graph.get(self.store,run['id'])
        self.assertNotIn('revised',available(self.store,run,self.store.now()))
        self.assertNotIn('revised',{row['step_id'] for row in candidates(self.store)})

    def test_late_diagnosis_cannot_stop_a_newer_running_revision(self):
        self.tick();task=script(retry_policy='never');task['on_failure']='diagnose'
        self.propose({'proposal_id':'stale-diagnosis','base_graph_version':0,'reason':'diagnose failure',
                      'add_tasks':[task],'sealed':True});run=self.tick()
        self.store.execute("UPDATE run_tasks SET status='paused',error_code='agent_failed' WHERE run_id=? AND id='first'",(run['id'],))
        run=graph.get(self.store,run['id']);self.orchestrator.queue_review(run,'first',purpose='diagnose')
        run=graph.get(self.store,run['id']);diagnosis=next(t for t in graph.work_items(run) if t.get('control_kind')=='DIAGNOSE')
        row=self.store.query("SELECT snapshot_digest FROM workflow_reviews WHERE id=?",(diagnosis['id'],))[0]
        self.store.execute("UPDATE run_tasks SET revision=1,status='running',error_code='',lease_id='new-attempt' WHERE run_id=? AND id='first'",(run['id'],))
        self.store.execute("UPDATE runs SET status='ACTIVE',terminal_reason='' WHERE id=?",(run['id'],))
        self.orchestrator.accept_diagnosis(graph.get(self.store,run['id']),diagnosis,{
            'snapshot_digest':row['snapshot_digest'],'diagnosis':{'facts':['old failure'],'uncertainties':[],
            'recommendations':['repair old revision'],'error_code':'agent_failed',
            'recommended_action':'stop','target_task_id':'first'}})
        current=graph.get(self.store,run['id']);subject=next(t for t in current['tasks'] if t['id']=='first')
        self.assertEqual((current['status'],current['terminal_reason']),('ACTIVE',''))
        self.assertEqual((subject['revision'],subject['status'],subject['lease_id']),(1,'running','new-attempt'))
        self.assertTrue(self.store.query("SELECT seq FROM workflow_graph_events WHERE kind='diagnosis_stale'"))

    def test_internal_model_transport_failure_retries_same_frozen_request_only(self):
        run=self.tick();manager=next(task for task in graph.work_items(run) if task.get('control_kind'))
        self.store.execute("UPDATE run_tasks SET status='running',attempts=1 WHERE run_id=? AND id=?",(run['id'],manager['id']))
        manager=next(task for task in graph.work_items(graph.get(self.store,run['id'])) if task['id']==manager['id'])
        self.orchestrator.task_failure(graph.get(self.store,run['id']),manager,
            {'error_code':'agent_failed','phase':'execute','classification':'execution','transient':True})
        current=graph.get(self.store,run['id']);retried=next(task for task in graph.work_items(current) if task['id']==manager['id'])
        self.assertEqual(retried['status'],'retry_wait')
        self.assertEqual(self.store.query('SELECT status FROM planner_runs WHERE task_id=?',(manager['id'],))[0]['status'],'pending')
        self.assertNotEqual(current['status'],'WAITING_USER')

    def test_sealed_failure_keeps_ready_diagnosis_dispatchable(self):
        self.tick();task=script(retry_policy='never');task['on_failure']='diagnose'
        self.propose({'proposal_id':'diagnose-failure','base_graph_version':0,'reason':'diagnose a known failure',
                      'add_tasks':[task],'sealed':True});run=self.tick()
        self.store.execute("UPDATE run_tasks SET status='running',attempts=1 WHERE run_id=? AND id='first'",(run['id'],))
        current=graph.get(self.store,run['id']);failed=next(t for t in graph.work_items(current) if t['id']=='first')
        self.orchestrator.task_failure(current,failed,{'error_code':'execution_failed','phase':'local','classification':'execution'})
        current=graph.get(self.store,run['id']);diagnosis=next(t for t in graph.work_items(current) if t.get('control_kind')=='DIAGNOSE')
        self.assertEqual((current['status'],current['terminal_reason']),('ACTIVE','failure_diagnosis'))
        self.assertEqual(diagnosis['status'],'ready')
        self.orchestrator.advance_graph(current,{t['id'] for t in graph.work_items(current) if t['status']=='succeeded'})
        current=graph.get(self.store,run['id'])
        self.assertEqual((current['status'],current['terminal_reason']),('ACTIVE','failure_diagnosis'))

    def test_interrupted_nonretryable_task_requires_controller_recovery(self):
        from webapp.task_leases import Coordinator
        self.tick();self.propose({'proposal_id':'one','base_graph_version':0,'reason':'no replay',
                                 'add_tasks':[script(retry_policy='never')],'sealed':True})
        self.tick();coordinator=Coordinator(self.store,'execution-test','test')
        lease=coordinator.claim(self.f.turn,'workflow',step_id='first');self.assertIsNotNone(lease)
        lease.release(interrupted=True)
        self.assertIsNone(coordinator.claim(self.f.turn,'workflow',step_id='first'))
        run=self.tick()
        self.assertEqual(run['status'],'WAITING_USER')
        self.assertEqual(next(t for t in graph.work_items(run) if t['id']=='first')['error_code'],'result_unavailable')

    def test_pause_does_not_apply_a_finished_manager_proposal(self):
        self.tick();self.propose({'proposal_id':'paused-plan','base_graph_version':0,'reason':'initial',
                                'add_tasks':[script()],'sealed':True})
        graph.command(self.store,self.run['id'],'owner','pause')
        run=self.tick();self.assertEqual(run['status'],'WAITING_USER');self.assertEqual(run['graph_version'],0)
        graph.command(self.store,self.run['id'],'owner','resume')
        run=self.tick();self.assertEqual(run['graph_version'],1)

    def test_revision_change_fences_the_old_executor_before_result_write(self):
        from webapp.task_leases import Coordinator,LeaseLost
        from webapp.workflow_dispatch import stage_result
        self.tick();self.propose({'proposal_id':'one','base_graph_version':0,'reason':'owned revision','add_tasks':[script()],'sealed':True})
        self.tick();lease=Coordinator(self.store,'revision-test','test').claim(self.f.turn,'workflow',step_id='first')
        self.assertIsNotNone(lease)
        try:
            self.store.execute("UPDATE run_tasks SET revision=revision+1 WHERE run_id=? AND id='first'",(self.run['id'],))
            with self.assertRaises(LeaseLost):stage_result(self.store,lease,'succeeded',{})
            self.assertFalse(self.store.query('SELECT id FROM task_results WHERE lease_id=?',(lease.attempt,)))
        finally:lease.release(interrupted=True)

    def test_invalid_candidate_is_rejected_without_reconcile_loop_or_delivery(self):
        from webapp.task_leases import Coordinator
        from webapp.workflow_dispatch import stage_result
        self.tick();self.propose({'proposal_id':'one','base_graph_version':0,'reason':'validate output','add_tasks':[script()],'sealed':True})
        self.tick();lease=Coordinator(self.store,'candidate-test','test').claim(self.f.turn,'workflow',step_id='first')
        self.assertIsNotNone(lease)
        try:
            with lease.bind():stage_result(self.store,lease,'succeeded',{'files':['edit/other-project/report.txt'],'quality':{'technical_valid':True,'passed':True,'files':[]}})
        finally:lease.release()
        run=self.tick();self.assertEqual(run['status'],'WAITING_USER');self.assertIsNone(run['delivered_at'])
        self.assertEqual(self.store.query('SELECT status FROM task_results')[0]['status'],'rejected')
        run=self.tick();self.assertEqual(run['status'],'WAITING_USER')

    def test_metadata_controller_cannot_read_or_upload_object_bytes(self):
        from unittest.mock import patch
        from webapp.artifact_store import ArtifactStore
        source=self.store.root/'file.txt';source.write_text('actual bytes')
        objects=ArtifactStore(self.store);record=objects.upload(self.f.project,source)
        objects.publish(self.f.project,'edit/original.txt',record['id'])
        with patch.dict('os.environ',{'VIDEO_USE_STORAGE_METADATA_ONLY':'1','VIDEO_USE_OBJECT_BUCKET':'private'}),patch('webapp.artifact_store.S3Objects',side_effect=AssertionError('Controller must not construct a credentialed backend')):
            metadata=ArtifactStore(self.store)
            self.assertEqual(metadata.record(self.f.project,record['id'])['sha256'],record['sha256'])
            metadata.publish(self.f.project,'edit/reference.txt',record['id'])
            with self.assertRaises(PermissionError):metadata.materialize(self.f.project,record['id'])
            with self.assertRaises(PermissionError):metadata.upload(self.f.project,source)
            with self.assertRaises(PermissionError):metadata.collect_garbage()
        self.assertEqual(len(self.store.query('SELECT id FROM artifact_objects')),1)

    def publish_metadata_fixture(self,names,primary_index=0,expect_success=True,selected_indices=None,
                                 dependencies=None,delivery_indices=None):
        """Exercise publication of previously accepted metadata, not decoding."""
        from unittest.mock import patch
        delivery_indices=set(range(len(names))) if delivery_indices is None else set(delivery_indices)
        self.tick();self.propose({'proposal_id':'publish-order','base_graph_version':0,'reason':'Deliver selected files',
            'add_tasks':[script('part_'+str(i),outputs=[name],output_roles={'result':name},delivery_stage=i in delivery_indices)
                         for i,name in enumerate(names)],
            'add_dependencies':dependencies or [],'sealed':True})
        self.tick();selected=[];hashes={}
        for index,name in enumerate(names):
            task='part_'+str(index);logical=f'edit/runs/{self.run["id"]}/{task}/output/{name}'
            source=self.store.root/f'accepted-{index}';source.write_bytes(('accepted-content-'+str(index)).encode())
            record=self.store.artifacts.upload(self.f.project,source);selected.append(logical);hashes[name]=record['sha256']
            manifest={logical:{'artifact':record['id'],'sha256':record['sha256'],'size':record['size']}}
            self.store.execute('INSERT INTO execution_step_checkpoints VALUES (?,?,?,?,?,?,?)',
                (self.run['id'],task,0,self.f.project,'accepted-attempt',graph.encode(manifest),self.store.now()))
            self.store.execute("UPDATE run_tasks SET status='succeeded',accepted_result_json=? WHERE run_id=? AND id=?",
                (graph.encode([logical]),self.run['id'],task))
        run=graph.get(self.store,self.run['id'])
        with patch.dict('os.environ',{'VIDEO_USE_OBJECT_BUCKET':'publication-metadata-fixture'}):
            reviewed=selected if selected_indices is None else [selected[index] for index in selected_indices]
            decision={'deliverables':reviewed}
            if primary_index is not None:decision['primary']=selected[primary_index]
            self.orchestrator.publish(run,{'decision_json':graph.encode(decision)})
            if not expect_success:
                result=graph.get(self.store,run['id'])
                self.assertEqual(result['status'],'WAITING_USER');self.assertIsNone(result['delivered_at']);return result['delivery'],hashes
            if primary_index is None:
                result=graph.get(self.store,run['id'])
                self.assertEqual(result['status'],'WAITING_USER');self.assertIsNone(result['delivered_at'])
                self.assertEqual(result['delivery'],[])
                self.assertFalse(self.store.query("SELECT seq FROM workflow_graph_events WHERE kind='delivered'"))
                return result['delivery'],hashes
            self.orchestrator.publish(graph.get(self.store,run['id']),{'decision_json':graph.encode({'deliverables':list(reversed(selected))})})
        result=graph.get(self.store,run['id'])
        self.assertEqual(result['status'],'SUCCEEDED')
        self.assertEqual(len(self.store.query("SELECT seq FROM workflow_graph_events WHERE kind='delivered'")),1)
        return result['delivery'],hashes

    def test_video_remains_primary_when_review_lists_cover_first(self):
        delivered,hashes=self.publish_metadata_fixture(['cover.png','final.mp4','review.json'],primary_index=1)
        self.assertEqual([a['name'] for a in delivered if a['is_primary']],['final.mp4'])
        self.assertEqual(delivered[0]['name'],'final.mp4')
        self.assertEqual({a['name']:a['sha256'] for a in delivered},hashes)

    def test_duplicate_display_names_do_not_create_multiple_primary_deliveries(self):
        delivered,_=self.publish_metadata_fixture(['image.png','image.png'])
        self.assertEqual(sum(a['is_primary'] for a in delivered),1)
        self.assertEqual(len({a['file'] for a in delivered}),2)

    def test_lossless_final_image_precedes_cover_and_compressed_rendition(self):
        delivered,_=self.publish_metadata_fixture(['cover.png','final.jpg','final.png'],primary_index=2)
        self.assertEqual([a['name'] for a in delivered if a['is_primary']],['final.png'])

    def test_explicit_primary_overrides_final_filename_and_duplicate_names(self):
        delivered,hashes=self.publish_metadata_fixture(['final.mp4','final.mp4','master_8s_horizontal.mp4'],primary_index=2)
        self.assertEqual(delivered[0]['name'],'master_8s_horizontal.mp4')
        self.assertEqual(delivered[0]['sha256'],hashes['master_8s_horizontal.mp4'])
        self.assertEqual(sum(a['is_primary'] for a in delivered),1)
        self.assertEqual(len({a['file'] for a in delivered}),3)

    def test_multiple_deliverables_without_primary_are_not_published(self):
        self.publish_metadata_fixture(['master_8s_horizontal.mp4','final.mp4'],primary_index=None)

    def test_explicit_file_delivery_targets_must_all_be_selected(self):
        authorization={**self.run['authorization'],'delivery_targets':['最终 PNG 及原始 TXT 记录']}
        self.store.execute('UPDATE runs SET goal=?,authorization_json=? WHERE id=?',
            ('交付 final.png 和 record.txt',graph.encode(authorization),self.run['id']))
        self.publish_metadata_fixture(['final.png'],expect_success=False)
        self.assertEqual(graph.get(self.store,self.run['id'])['terminal_reason'],'delivery_targets_incomplete')
        payload=json.loads(self.store.query("SELECT payload_json FROM workflow_graph_events WHERE kind='delivery_blocked'")[0]['payload_json'])
        self.assertEqual(payload['missing_extensions'],['txt'])
        self.assertEqual(payload['missing_task_ids'],[])

    def test_explicit_delivery_filename_cannot_be_replaced_by_same_format(self):
        authorization={**self.run['authorization'],'delivery_targets':['final.mp4 and cover.png']}
        self.store.execute('UPDATE runs SET goal=?,authorization_json=? WHERE id=?',
            ('Deliver final.mp4 and cover.png',graph.encode(authorization),self.run['id']))
        self.publish_metadata_fixture(['clip_128x64.mp4','cover.png'],primary_index=0,expect_success=False)
        payload=json.loads(self.store.query("SELECT payload_json FROM workflow_graph_events WHERE kind='delivery_blocked'")[0]['payload_json'])
        self.assertEqual(payload['missing_names'],['final.mp4'])

    def test_explicit_report_role_must_be_selected(self):
        authorization={**self.run['authorization'],'delivery_targets':['final report']}
        self.store.execute('UPDATE runs SET goal=?,authorization_json=? WHERE id=?',
            ('Deliver final.png and an objective report',graph.encode(authorization),self.run['id']))
        self.publish_metadata_fixture(['final.png','report.json'],expect_success=False)
        payload=json.loads(self.store.query("SELECT payload_json FROM workflow_graph_events WHERE kind='delivery_blocked'")[0]['payload_json'])
        self.assertEqual(payload['missing_roles'],['report'])

    def test_input_asset_extension_is_not_treated_as_a_delivery_format(self):
        authorization={**self.run['authorization'],'delivery_targets':['符合需求的 512x512 图片']}
        self.store.execute('UPDATE runs SET goal=?,authorization_json=? WHERE id=?',
            ('将 source.png 转为 JPEG，保持内容不变',graph.encode(authorization),self.run['id']))
        delivered,_=self.publish_metadata_fixture(['final.jpg'])
        self.assertEqual([item['name'] for item in delivered],['final.jpg'])

    def test_each_delivery_stage_leaf_must_contribute_a_selected_file(self):
        self.publish_metadata_fixture(['landscape.mp4','portrait.mp4'],primary_index=0,
                                      expect_success=False,selected_indices=[0])
        self.assertEqual(graph.get(self.store,self.run['id'])['terminal_reason'],'delivery_targets_incomplete')
        payload=json.loads(self.store.query("SELECT payload_json FROM workflow_graph_events WHERE kind='delivery_blocked'")[0]['payload_json'])
        self.assertEqual(payload['missing_task_ids'],['part_1'])
        self.assertEqual(payload['missing_extensions'],[])

    def test_delivery_leaf_detection_traverses_non_delivery_tasks(self):
        delivered,_=self.publish_metadata_fixture(['transparent.png','layout.json','poster.png'],primary_index=2,
            selected_indices=[2],delivery_indices=[0,2],dependencies=[
                {'upstream':'part_0','downstream':'part_1'},
                {'upstream':'part_1','downstream':'part_2'}])
        self.assertEqual([item['name'] for item in delivered],['poster.png'])

    def test_primary_selection_is_validated_by_worker_and_controller(self):
        from webapp.workflow_agents import validated_internal_result
        from webapp.specialist_profiles import get
        run=self.review_fixture()
        self.store.execute("UPDATE run_tasks SET status='succeeded',accepted_result_json=? WHERE run_id=? AND id='first'",
            (graph.encode(['edit/master.mp4','edit/final.mp4']),run['id']))
        run=graph.get(self.store,run['id']);self.orchestrator.queue_review(run)
        row=self.store.query("SELECT * FROM workflow_reviews WHERE purpose='final'")[0]
        task=next(t for t in graph.work_items(graph.get(self.store,run['id'])) if t['id']==row['task_id'])
        frozen={**json.loads(row['snapshot_json']),'snapshot_digest':row['snapshot_digest']}
        selected=['edit/master.mp4','edit/final.mp4']
        ids=['a'*32,'b'*32];inspections=dict(zip(ids,selected));keys={'delivery_1':ids[0],'delivery_2':ids[1]}
        decision={'snapshot_digest':row['snapshot_digest'],'verdict':'pass','deliverables':list(keys),
                  'evidence':[{'criterion_index':0,'observation':'Checked both variants','inspection_id':ids[0]}]}
        with self.assertRaisesRegex(ValueError,'[Pp]rimary'):
            validated_internal_result(run,task,get('delivery_reviewer'),{'decision':decision},frozen,inspections,keys)
        with self.assertRaisesRegex(ValueError,'candidate_key'):
            validated_internal_result(run,task,get('delivery_reviewer'),{'decision':{**decision,'primary':'f'*32}},frozen,inspections,keys)
        self.assertEqual(self.store.query('SELECT status FROM workflow_reviews')[0]['status'],'pending')
        valid={**decision,'primary':'delivery_1'}
        with self.assertRaisesRegex(ValueError,'inspection_id'):
            validated_internal_result(run,task,get('delivery_reviewer'),{'decision':valid},frozen,{ids[1]:selected[1]},keys)
        for identifier,reference in inspections.items():
            self.store.execute('INSERT INTO workflow_inspections VALUES (?,?,?,?,?,?,?,?)',
                (identifier,run['id'],task['id'],task['lease_id'],reference,row['snapshot_digest'],'{}',self.store.now()))
        resolved=validated_internal_result(run,task,get('delivery_reviewer'),{'decision':valid},frozen,inspections,keys)['decision']
        self.assertEqual(resolved['primary'],selected[0])
        by_ref={**valid,'deliverables':selected,'primary':selected[0]}
        self.assertEqual(validated_internal_result(run,task,get('delivery_reviewer'),{'decision':by_ref},frozen,inspections,keys)['decision']['primary'],selected[0])
        by_name={**valid,'deliverables':['master.mp4','final.mp4'],'primary':'master.mp4'}
        self.assertEqual(validated_internal_result(run,task,get('delivery_reviewer'),{'decision':by_name},frozen,inspections,keys)['decision']['primary'],selected[0])
        exact_name={**frozen,'user_changes':[{'content':'Keep both variants but deliver requested.mp4 as the primary video.'}]}
        with self.assertRaisesRegex(ValueError,'required filenames'):
            validated_internal_result(run,task,get('delivery_reviewer'),{'decision':valid},exact_name,inspections,keys)
        only={'delivery_1':ids[0]};automatic={**valid,'deliverables':[],'primary':''}
        resolved=validated_internal_result(run,task,get('delivery_reviewer'),{'decision':automatic},frozen,{ids[0]:selected[0]},only)['decision']
        self.assertEqual((resolved['deliverables'],resolved['primary']),([selected[0]],selected[0]))
        self.orchestrator.accept_review(run,task,{'decision':resolved})
        accepted=self.store.query('SELECT status,decision_json FROM workflow_reviews')[0]
        self.assertEqual(accepted['status'],'passed');self.assertEqual(json.loads(accepted['decision_json'])['primary'],selected[0])

    def review_fixture(self,user_review=False):
        self.tick();self.propose({'proposal_id':'review-source','base_graph_version':0,'reason':'Source',
                                'add_tasks':[script(acceptance=[{'kind':'file'}]+([{'kind':'review','criterion':'User content confirmation'}] if user_review else []))],'sealed':True})
        self.tick()
        self.store.execute("UPDATE run_tasks SET status='reviewing',phase='validate',candidate_result_json=?,quality_json=? WHERE run_id=? AND id='first'",
            (graph.encode(['edit/report.txt']),graph.encode({'technical_valid':True,'passed':True}),self.run['id']))
        return graph.get(self.store,self.run['id'])

    def test_intermediate_review_is_bound_to_step_criteria_not_final_delivery(self):
        run=self.review_fixture();subject=next(t for t in graph.work_items(run) if t['id']=='first')
        criteria=['自然暖色，保持主体完整','中间文件声音保留']
        spec={**subject['spec'],'objective':'生成暖色中间母版；字幕在后续步骤添加',
              'acceptance':[{'kind':'agent','criterion':c} for c in criteria]}
        self.store.execute('UPDATE run_tasks SET spec_json=? WHERE run_id=? AND id=?',
            (graph.encode(spec),run['id'],'first'))
        run=graph.get(self.store,run['id']);self.orchestrator.queue_review(run,'first')
        row=self.store.query("SELECT * FROM workflow_reviews WHERE purpose='task'")[0]
        frozen=json.loads(row['snapshot_json']);self.assertEqual(frozen['goal'],spec['objective'])
        self.assertEqual(frozen['review_criteria'],criteria);self.assertNotIn('delivery_targets',frozen)
        task=next(t for t in graph.work_items(graph.get(self.store,run['id'])) if t['id']==row['task_id'])
        self.assertEqual(task['spec']['objective'],spec['objective'])
        from webapp.workflow_agents import validated_internal_result
        from webapp.specialist_profiles import get
        inspection='c'*32
        invalid={'snapshot_digest':row['snapshot_digest'],'verdict':'reject','evidence':[
            {'criterion_index':99,'observation':'Final subtitles are missing','inspection_id':inspection}]}
        with self.assertRaisesRegex(ValueError,'Step review evidence'):
            validated_internal_result(run,task,get('visual_quality_reviewer'),{'decision':invalid},
                {**frozen,'snapshot_digest':row['snapshot_digest']},{inspection:'edit/report.txt'})
        self.assertEqual(self.store.query('SELECT status FROM workflow_reviews')[0]['status'],'pending')
        valid={**invalid,'verdict':'pass','evidence':[{'criterion_index':index,'observation':'Inspected this step','inspection_id':inspection} for index in range(len(criteria))]}
        with self.assertRaisesRegex(ValueError,'every frozen'):
            validated_internal_result(run,task,get('visual_quality_reviewer'),{'decision':{**valid,'evidence':valid['evidence'][:1]}},
                {**frozen,'snapshot_digest':row['snapshot_digest']},{inspection:'edit/report.txt'})
        self.store.execute('INSERT INTO workflow_inspections VALUES (?,?,?,?,?,?,?,?)',
            (inspection,run['id'],task['id'],task['lease_id'],'edit/report.txt',row['snapshot_digest'],'{}',self.store.now()))
        resolved=validated_internal_result(run,task,get('visual_quality_reviewer'),{'decision':valid},
            {**frozen,'snapshot_digest':row['snapshot_digest']},{inspection:'edit/report.txt'})['decision']
        self.orchestrator.accept_review(run,task,{'decision':resolved})
        current=graph.get(self.store,run['id']);self.assertEqual(next(t for t in graph.work_items(current) if t['id']=='first')['status'],'succeeded')
        self.orchestrator.queue_review(current)
        final=json.loads(self.store.query("SELECT snapshot_json FROM workflow_reviews WHERE purpose='final'")[0]['snapshot_json'])
        self.assertEqual(final['review_scope'],'final');self.assertEqual(final['goal'],run['goal'])
        self.assertEqual(final['delivery_targets'],run['authorization']['delivery_targets'])

    def test_rejected_step_review_routes_to_manager_and_cannot_be_resumed_as_retry(self):
        run=self.review_fixture();self.orchestrator.queue_review(run,'first')
        current=graph.get(self.store,run['id']);review=next(t for t in graph.work_items(current) if t.get('control_kind') and t['spec'].get('profile')=='visual_quality_reviewer')
        self.orchestrator.accept_review(current,review,{'decision':{'snapshot_digest':review['spec']['snapshot_digest'],
            'verdict':'reject','feedback':'The actual evidence does not meet the criterion.'}})
        rejected=graph.get(self.store,run['id']);subject=next(t for t in rejected['tasks'] if t['id']=='first')
        self.assertEqual(subject['status'],'paused');self.assertEqual(subject['error_code'],'quality_failed')
        self.assertFalse(subject['accepted_result'])
        graph.command(self.store,run['id'],'owner','resume')
        self.assertEqual(self.tick()['status'],'ACTIVE')
        self.assertEqual(self.store.query("SELECT status FROM workflow_commands WHERE action='resume'")[0]['status'],'rejected')
        self.assertEqual(len(self.store.query("SELECT id FROM planner_runs WHERE status='pending'")),1)

    def test_repair_revision_invalidates_descendants_and_preserves_unrelated_branch(self):
        self.tick();self.propose({'proposal_id':'initial-repair','base_graph_version':0,'reason':'initial',
            'add_tasks':[script('first'),script('child'),script('unrelated')],
            'add_dependencies':[{'upstream':'first','downstream':'child'}],'sealed':True})
        run=self.tick()
        self.store.execute("UPDATE run_tasks SET status='paused',error_code='execution_failed' WHERE run_id=? AND id='first'",(run['id'],))
        self.store.execute("UPDATE run_tasks SET status='succeeded' WHERE run_id=? AND id IN ('child','unrelated')",(run['id'],))
        self.store.execute("UPDATE runs SET sealed=0,status='ACTIVE' WHERE id=?",(run['id'],))
        planning.queue(self.store,graph.get(self.store,run['id']),{'task_id':'first','repair_policy':'manager_revision'})
        replacement=script('first',argv=['python','-c','print("repaired")'])
        self.propose({'proposal_id':'repair-first','base_graph_version':1,'reason':'replace failed local implementation',
            'revise_tasks':[replacement],'sealed':True})
        repaired=self.tick();tasks={task['id']:task for task in graph.work_items(repaired) if not task.get('control_kind')}
        self.assertEqual((tasks['first']['revision'],tasks['first']['repair_count'],tasks['first']['status']),(1,1,'ready'))
        self.assertEqual((tasks['child']['revision'],tasks['child']['repair_count'],tasks['child']['status']),(1,0,'blocked'))
        self.assertEqual((tasks['unrelated']['revision'],tasks['unrelated']['repair_count'],tasks['unrelated']['status']),(0,0,'succeeded'))
        history=self.store.query('SELECT task_id,revision,repair_root FROM workflow_task_revisions WHERE run_id=? ORDER BY task_id,revision',(run['id'],))
        self.assertEqual([(row['task_id'],row['revision'],row['repair_root']) for row in history],
            [('child',0,0),('child',1,0),('first',0,0),('first',1,1),('unrelated',0,0)])
        selected=self.store.query('SELECT task_id,task_revision FROM workflow_plan_tasks WHERE run_id=? AND graph_version=2 ORDER BY task_id',(run['id'],))
        self.assertEqual([(row['task_id'],row['task_revision']) for row in selected],[('child',1),('first',1),('unrelated',0)])

    def test_revision_and_new_descendant_commit_atomically_without_revising_new_task(self):
        self.tick();self.propose({'proposal_id':'mixed-initial','base_graph_version':0,'reason':'initial',
            'add_tasks':[script('first'),script('unrelated')],'sealed':True})
        run=self.tick()
        self.store.execute("UPDATE run_tasks SET status='paused',error_code='execution_failed' WHERE run_id=? AND id='first'",(run['id'],))
        self.store.execute("UPDATE run_tasks SET status='succeeded' WHERE run_id=? AND id='unrelated'",(run['id'],))
        self.store.execute("UPDATE runs SET sealed=0,status='ACTIVE' WHERE id=?",(run['id'],))
        planning.queue(self.store,graph.get(self.store,run['id']),{'task_id':'first'})
        self.propose({'proposal_id':'mixed-repair','base_graph_version':1,'reason':'repair and add dependent',
            'revise_tasks':[script('first',argv=['python','-c','print("fixed")'])],
            'add_tasks':[script('new_child',inputs={'source':{'step':'first','role':'report'}})],
            'add_dependencies':[{'upstream':'first','downstream':'new_child'}],'sealed':True})
        result=self.tick();tasks={t['id']:t for t in result['tasks']}
        self.assertEqual(result['graph_version'],2)
        self.assertEqual(tasks['first']['revision'],1)
        self.assertEqual((tasks['new_child']['revision'],tasks['new_child']['status']),(0,'blocked'))
        self.assertEqual((tasks['unrelated']['revision'],tasks['unrelated']['status']),(0,'succeeded'))
        history=self.store.query("SELECT revision FROM workflow_task_revisions WHERE run_id=? AND task_id='new_child'",(run['id'],))
        self.assertEqual([row['revision'] for row in history],[0])
        self.assertFalse(self.store.query("SELECT id FROM planner_runs WHERE run_id=? AND status='staged'",(run['id'],)))

    def test_repair_context_survives_an_intermediate_diagnostic_planning_round(self):
        self.tick();self.propose({'proposal_id':'initial-durable-repair','base_graph_version':0,'reason':'initial',
            'add_tasks':[script('first'),script('child')],'add_dependencies':[{'upstream':'first','downstream':'child'}],
            'sealed':True});run=self.tick()
        self.store.execute("UPDATE run_tasks SET status='paused',error_code='quality_failed' WHERE run_id=? AND id='first'",(run['id'],))
        self.store.execute("UPDATE runs SET sealed=0,status='ACTIVE' WHERE id=?",(run['id'],))
        run=graph.get(self.store,run['id']);self.orchestrator.open_repair(run,'first','task_review','review-id','quality_failed')
        planning.queue(self.store,graph.get(self.store,run['id']),{'task_id':'first','repair_policy':'manager_revision'})
        self.propose({'proposal_id':'diagnose-before-repair','base_graph_version':1,'reason':'collect bounded evidence',
            'add_tasks':[script('diagnosis')],'replan_after':['diagnosis']})
        intermediate=self.tick();self.assertTrue(intermediate['repair_context']['active'])
        self.store.execute("UPDATE run_tasks SET status='succeeded' WHERE run_id=? AND id='diagnosis'",(run['id'],))
        self.tick()
        replacement=script('first',argv=['python','-c','print("repaired after diagnosis")'])
        self.propose({'proposal_id':'repair-after-diagnosis','base_graph_version':2,'reason':'apply diagnosed repair',
            'revise_tasks':[replacement],'sealed':True})
        repaired=self.tick();tasks={task['id']:task for task in graph.work_items(repaired) if not task.get('control_kind')}
        self.assertEqual((tasks['first']['revision'],tasks['child']['revision']),(1,1))
        self.assertEqual(repaired['repair_context'],{})

    def test_sealed_repair_cannot_abandon_a_paused_required_task(self):
        self.tick();self.propose({'proposal_id':'initial-required','base_graph_version':0,'reason':'initial',
            'add_tasks':[script('first')],'sealed':True});run=self.tick()
        self.store.execute("UPDATE run_tasks SET status='paused',error_code='quality_failed' WHERE run_id=? AND id='first'",(run['id'],))
        self.store.execute("UPDATE runs SET sealed=0,status='ACTIVE' WHERE id=?",(run['id'],))
        run=graph.get(self.store,run['id']);self.orchestrator.open_repair(run,'first','task_review','review-id','quality_failed')
        with self.assertRaisesRegex(ValueError,'resolve paused tasks'):
            graph.validate_proposal(self.store,graph.get(self.store,run['id']),{'proposal_id':'abandon-paused','base_graph_version':1,
                'reason':'add a replacement without repairing the required task','add_tasks':[script('replacement')],'sealed':True},repair_scope='first')

    def test_rejected_final_review_is_visible_in_the_next_frozen_planning_input(self):
        run=self.review_fixture()
        self.store.execute("UPDATE run_tasks SET status='succeeded',accepted_result_json=candidate_result_json WHERE run_id=? AND id='first'",(run['id'],))
        run=graph.get(self.store,run['id']);self.orchestrator.queue_review(run)
        current=graph.get(self.store,run['id']);review=next(t for t in graph.work_items(current) if t.get('control_kind') and t['spec'].get('profile')=='delivery_reviewer')
        self.orchestrator.accept_review(current,review,{'decision':{'snapshot_digest':review['spec']['snapshot_digest'],
            'verdict':'reject','feedback':'Required ending is missing.'}})
        row=self.store.query("SELECT snapshot_json FROM planner_runs WHERE run_id=? AND status='pending'",(run['id'],))[0]
        frozen=json.loads(row['snapshot_json'])
        self.assertEqual(frozen['trigger']['decision']['feedback'],'Required ending is missing.')
        self.assertEqual(frozen['review_feedback'][0]['decision']['feedback'],'Required ending is missing.')
        self.assertIsNone(graph.get(self.store,run['id'])['delivered_at'])

    def test_successor_applies_consumed_inputs_and_keeps_late_inputs_pending(self):
        from webapp.run_inputs import accept
        first=accept(self.store,self.run['id'],'owner',{'request_id':'1'*32,'content':'New overall goal'})
        second=accept(self.store,self.run['id'],'owner',{'request_id':'2'*32,'content':'Late planning detail'})
        authorization={**self.authorization,'goal':'New overall goal'}
        request={'new_goal':'New overall goal','authorization':authorization,'input_through_sequence':first['sequence']}
        self.orchestrator.decision(graph.get(self.store,self.run['id']),'new_run',request)
        decision=self.store.query("SELECT * FROM workflow_decisions WHERE kind='new_run'")[0]
        self.orchestrator.answer(graph.get(self.store,self.run['id']),{'decision_id':decision['id'],
            'snapshot_digest':decision['snapshot_digest'],'action':'approve','authorization':authorization})
        successor=graph.get(self.store,self.store.query('SELECT id FROM runs WHERE parent_run_id=?',(self.run['id'],))[0]['id'])
        carried=self.store.query('SELECT sequence,status,applied_graph_version,context_json FROM run_inputs WHERE run_id=? ORDER BY sequence',(successor['id'],))
        self.assertEqual([(row['sequence'],row['status']) for row in carried],[(1,'APPLIED'),(2,'ACCEPTED')])
        self.assertEqual(carried[0]['applied_graph_version'],0)
        self.assertEqual(successor['input_barrier_sequence'],2)
        event=json.loads(self.store.query("SELECT payload_json FROM workflow_graph_events WHERE run_id=? AND kind='input_batch_applied'",(successor['id'],))[0]['payload_json'])
        self.assertEqual(event,{'through_sequence':1,'next_barrier_sequence':2})
        original=self.store.query('SELECT status,successor_run_id FROM run_inputs WHERE run_id=? ORDER BY sequence',(self.run['id'],))
        self.assertTrue(all(row['status']=='SUPERSEDED' and row['successor_run_id']==successor['id'] for row in original))

    def test_user_rejecting_quality_also_pauses_the_subject(self):
        run=self.review_fixture();self.orchestrator.decision(run,'quality',{'task_id':'first','revision':0})
        decision=self.store.query('SELECT * FROM workflow_decisions')[0]
        self.orchestrator.answer(graph.get(self.store,run['id']),{'decision_id':decision['id'],
            'snapshot_digest':decision['snapshot_digest'],'action':'reject'})
        subject=next(t for t in graph.work_items(graph.get(self.store,run['id'])) if t['id']=='first')
        self.assertEqual(subject['status'],'paused');self.assertEqual(subject['error_code'],'quality_failed')

    def test_error_pause_keeps_its_reason_without_a_user_pause_command(self):
        self.store.execute("UPDATE runs SET status='WAITING_USER',terminal_reason='budget_exhausted' WHERE id=?",(self.run['id'],))
        self.assertEqual(self.tick()['terminal_reason'],'budget_exhausted')
        self.assertEqual(self.tick()['terminal_reason'],'budget_exhausted')

    def test_resume_from_user_pause_returns_to_pending_confirmation_without_dispatch(self):
        run=self.review_fixture();self.orchestrator.decision(run,'quality',{'task_id':'first','revision':0})
        graph.command(self.store,run['id'],'owner','pause');self.assertEqual(self.tick()['status'],'WAITING_USER')
        graph.command(self.store,run['id'],'owner','resume');self.assertEqual(self.tick()['status'],'WAITING_USER')
        self.assertEqual(self.store.query("SELECT status FROM workflow_commands WHERE action='resume'")[0]['status'],'applied')
        from webapp.workflow_dispatch import candidates
        self.assertFalse(candidates(self.store))

    def test_parallel_manual_reviews_are_presented_after_each_prior_confirmation(self):
        self.tick();self.propose({'proposal_id':'manual-two','base_graph_version':0,'reason':'Two independent candidates',
            'add_tasks':[script(name,acceptance=[{'kind':'file'},{'kind':'review','criterion':'User content confirmation'}]) for name in ('first','second')],'sealed':True});self.tick()
        self.store.execute("UPDATE run_tasks SET status='reviewing',phase='validate',candidate_result_json=?,quality_json=? WHERE run_id=?",
            (graph.encode(['edit/report.txt']),graph.encode({'technical_valid':True,'passed':True,'requires_review':True}),self.run['id']))
        run=graph.get(self.store,self.run['id']);self.orchestrator.advance(run)
        decision=self.store.query("SELECT * FROM workflow_decisions WHERE status='pending'")[0]
        first=json.loads(decision['request_json'])['task_id']
        self.orchestrator.answer(graph.get(self.store,run['id']),{'decision_id':decision['id'],'snapshot_digest':decision['snapshot_digest'],'action':'approve'})
        self.orchestrator.advance(graph.get(self.store,run['id']))
        pending=self.store.query("SELECT * FROM workflow_decisions WHERE status='pending'")
        self.assertEqual(len(pending),1)
        self.assertNotEqual(json.loads(pending[0]['request_json'])['task_id'],first)
        self.assertEqual(graph.get(self.store,run['id'])['status'],'WAITING_USER')
        self.assertIsNone(graph.get(self.store,run['id'])['delivered_at'])

    def test_agent_and_user_criteria_both_hold_before_accepting_the_candidate(self):
        run=self.review_fixture(user_review=True)
        self.store.execute('UPDATE run_tasks SET quality_json=? WHERE run_id=? AND id=?',
            (graph.encode({'technical_valid':True,'passed':True,'requires_review':True,'requires_agent':True}),run['id'],'first'))
        run=graph.get(self.store,run['id']);self.orchestrator.queue_review(run,'first');self.orchestrator.advance(run)
        self.assertFalse(self.store.query('SELECT id FROM workflow_decisions'))
        current=graph.get(self.store,run['id']);review=next(t for t in graph.work_items(current) if t.get('control_kind') and t['spec'].get('profile')=='visual_quality_reviewer')
        self.store.execute('INSERT INTO workflow_inspections VALUES (?,?,?,?,?,?,?,?)',('d'*32,run['id'],review['id'],review['lease_id'],'edit/report.txt',review['spec']['snapshot_digest'],'{}',self.store.now()))
        self.orchestrator.accept_review(current,review,{'decision':{'snapshot_digest':review['spec']['snapshot_digest'],'verdict':'pass',
            'evidence':[{'criterion':'满足当前步骤目标及输出契约','observation':'Checked actual candidate','ref':'edit/report.txt','inspection_id':'d'*32}]}})
        subject=next(t for t in graph.work_items(graph.get(self.store,run['id'])) if t['id']=='first')
        self.assertEqual(subject['status'],'reviewing');self.assertFalse(subject['accepted_result'])
        decision=self.store.query("SELECT * FROM workflow_decisions WHERE status='pending'")[0]
        self.orchestrator.answer(graph.get(self.store,run['id']),{'decision_id':decision['id'],'snapshot_digest':decision['snapshot_digest'],'action':'approve'})
        subject=next(t for t in graph.work_items(graph.get(self.store,run['id'])) if t['id']=='first')
        self.assertEqual(subject['status'],'succeeded');self.assertEqual(subject['accepted_result'],['edit/report.txt'])

    def test_scope_approval_uses_worker_implementation_without_controller_credentials(self):
        from unittest.mock import patch
        from webapp.operations import get
        worker_operation=get('image.generate_from_text','image_sync@1').public()
        raw={**self.authorization,'allowed_operations':['image.generate_from_text']}
        with patch.dict('os.environ',{'VIDEO_USE_STORAGE_METADATA_ONLY':'1'},clear=True), \
             patch('webapp.workflow_planning.available_operations',return_value=[worker_operation]):
            bound=planning.bind_scope(self.store,raw)
        self.assertEqual(bound.operation_implementations,{'image.generate_from_text':'image_sync@1'})
        with patch('webapp.workflow_planning.available_operations',return_value=None),self.assertRaises(ValueError):
            planning.bind_scope(self.store,raw)

    def test_malformed_decision_command_cannot_block_later_cancellation(self):
        bad=graph.command(self.store,self.run['id'],'owner','answer',{'decision_id':{},'action':'approve'})
        graph.command(self.store,self.run['id'],'owner','cancel')
        run=self.tick();self.assertEqual(run['status'],'CANCELLED')
        row=self.store.query('SELECT status,error_code FROM workflow_commands WHERE id=?',(bad['id'],))[0]
        self.assertEqual(row['status'],'rejected');self.assertEqual(row['error_code'],'invalid_or_stale_decision')

    def test_rejected_proposal_id_cannot_turn_a_new_request_into_database_failure(self):
        self.tick()
        self.propose({'proposal_id':'reused-id','base_graph_version':0,'reason':'cycle','add_tasks':[script('a'),script('b')],
          'add_dependencies':[{'upstream':'a','downstream':'b'},{'upstream':'b','downstream':'a'}],'sealed':True})
        self.tick()
        request=self.store.query("SELECT * FROM planner_runs WHERE status='pending'")[0]
        from webapp.task_leases import Coordinator
        lease=Coordinator(self.store,'new-plan','test').claim(self.f.turn,'workflow',step_id=request['task_id'])
        try:
            with lease.bind(),self.assertRaises(planning.ProposalIdentityError):
                planning.stage(self.store,lease,request['id'],envelope(self.store,request['id'],{'proposal_id':'reused-id','base_graph_version':0,'reason':'corrected','add_tasks':[script()],'sealed':True}))
            self.assertEqual(self.store.query('SELECT status FROM planner_runs WHERE id=?',(request['id'],))[0]['status'],'running')
            frozen=json.loads(request['snapshot_json']);self.assertIn('reused-id',frozen['used_proposal_ids'])
            self.assertEqual(frozen['planning_request_id'],request['id'])
        finally:lease.release()
