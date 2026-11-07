"""Recovery budgets count interruptions, not normal poll dispatches."""
import unittest
from tests import test_dynamic_graph as fixtures
from webapp import workflow_graph as graph
from webapp.task_leases import Coordinator
from webapp.workflow_executor import stage_result


class RunRecoveryTests(unittest.TestCase):
    def setUp(self):
        self.f=fixtures.DynamicGraphTests();self.f.setUp();self.addCleanup(self.f.tearDown)
        self.store=self.f.store;self.f.tick()
        self.f.propose({'proposal_id':'base','base_graph_version':0,'reason':'Recovery fixture',
                        'add_tasks':[fixtures.script('render')],'sealed':True})
        self.run=self.f.tick()

    def provider_task(self):
        spec=graph.Task.model_validate({'id':'render','type':'operation','operation':'video.generate_from_image',
            'name':'Render','objective':'Generate approved video','inputs':{'source':{'asset':'asset'}},
            'parameters':{'prompt':'A gentle camera movement','width':1280,'height':720,'duration':5},
            'acceptance':[{'kind':'file'}],'basic_reason':'Recovery protocol fixture; content review is tested separately','delivery_stage':True}).model_dump()
        self.store.execute("UPDATE run_tasks SET type='operation',spec_json=?,phase='poll',status='running',attempts=100,recovery_count=2,external_id='original-job',lease_id='lost-attempt' WHERE run_id=?",(graph.encode(spec),self.run['id']))
        self.store.execute('''INSERT INTO workflow_provider_calls(request_key,workflow,step,revision,attempt,provider,operation,state,external_id,inputs,created,updated)
            VALUES (?,?,?,0,1,?,'video.generate_from_image','running','original-job','{}',?,?)''',
            (self.run['id']+':render:0',self.run['id'],'render','https://provider.invalid',self.store.now(),self.store.now()))

    def test_many_healthy_polls_do_not_exhaust_restart_recovery(self):
        self.provider_task();self.f.orchestrator.recover(graph.get(self.store,self.run['id']))
        run=graph.get(self.store,self.run['id']);task=run['tasks'][0]
        self.assertEqual((task['status'],task['phase'],task['external_id'],task['attempts'],task['recovery_count'],task['infrastructure_interruptions']),
                         ('ready','poll','original-job',100,2,1))
        lease=Coordinator(self.store,'poll-recovery','test').claim(run['id'],'workflow',step_id='render')
        self.assertIsNotNone(lease)
        try:
            with lease.bind():stage_result(self.store,lease,'waiting',{'external_id':'original-job','poll_after':2})
        finally:lease.release()
        self.f.orchestrator.accept_results(graph.get(self.store,run['id']))
        task=graph.get(self.store,run['id'])['tasks'][0]
        self.assertEqual((task['status'],task['recovery_count']),('waiting',0))
        self.assertEqual(len(self.store.query('SELECT request_key FROM workflow_provider_calls')),1)

    def test_terminal_provider_failure_is_not_polled_again(self):
        self.provider_task()
        self.store.execute("UPDATE workflow_provider_calls SET state='failed' WHERE workflow=?",(self.run['id'],))
        self.f.orchestrator.recover(graph.get(self.store,self.run['id']))
        task=graph.get(self.store,self.run['id'])['tasks'][0]
        self.assertEqual((task['status'],task['error_code']),('paused','provider_failed'))
        self.assertFalse(self.store.query("SELECT id FROM task_results WHERE run_id=? AND outcome='waiting'",(self.run['id'],)))

    def test_succeeded_provider_result_recovers_at_validation(self):
        self.provider_task()
        self.store.execute("UPDATE workflow_provider_calls SET state='succeeded',result='[\"edit/final.mp4\"]' WHERE workflow=?",(self.run['id'],))
        self.f.orchestrator.recover(graph.get(self.store,self.run['id']))
        task=graph.get(self.store,self.run['id'])['tasks'][0]
        self.assertEqual((task['status'],task['phase'],task['candidate_result']),('ready','validate',['edit/final.mp4']))

    def test_interrupted_model_reservation_converges_to_bounded_unknown(self):
        import uuid
        attempt=uuid.uuid4().hex;now=self.store.now()
        self.store.execute("INSERT INTO execution_attempts(id,run_id,worker,generation,kind,started,finished,status,exit_reason,step_id) VALUES (?,?,?,?,?,?,?,?,?,?)",
            (attempt,self.run['id'],'dead-worker',9,'workflow',now-2,now-1,'interrupted','interrupted','render'))
        self.store.execute("INSERT INTO workflow_model_reservations(id,run_id,attempt_id,reserved_tokens,state,created_at,input_reserved_tokens,output_reserved_tokens) VALUES (?,?,?,?,?,?,?,?)",
            (uuid.uuid4().hex,self.run['id'],attempt,9000,'reserved',now-2,3000,6000))
        self.f.orchestrator.recover(graph.get(self.store,self.run['id']))
        row=self.store.query('SELECT state,used_tokens,input_settled_tokens,output_settled_tokens FROM workflow_model_reservations')[0]
        self.assertEqual((row['state'],row['used_tokens'],row['input_settled_tokens'],row['output_settled_tokens']),
                         ('interrupted_usage_unknown',9000,None,None))

    def test_runtime_change_opens_repair_instead_of_silent_ready_loop(self):
        self.store.execute("UPDATE run_tasks SET status='ready',bindings_json=? WHERE run_id=? AND id='render'",
                           (graph.encode({'_environment':'obsolete-runtime'}),self.run['id']))
        self.f.orchestrator.recover(graph.get(self.store,self.run['id']))
        run=graph.get(self.store,self.run['id']);task=run['tasks'][0]
        self.assertEqual((task['status'],task['error_code']),('paused','runtime_profile_changed'))
        self.assertTrue(run['repair_context']['active'])
        self.assertTrue(any(job['control_kind']=='PLAN' and job['status']=='ready' for job in run['control_jobs']))

    def test_dynamic_resource_wait_preserves_backoff_without_spending_interruption_budget(self):
        from webapp.scheduling import defer_resource
        coordinator=Coordinator(self.store,'resource-wait','test')
        lease=coordinator.claim(self.run['id'],'workflow',step_id='render');self.assertIsNotNone(lease)
        defer_resource(lease,30);lease.release()
        self.f.orchestrator.recover(graph.get(self.store,self.run['id']))
        task=graph.get(self.store,self.run['id'])['tasks'][0]
        self.assertEqual((task['status'],task['lease_id'],task['wait_reason'],task['infrastructure_interruptions']),
                         ('ready','','resource',0))
        self.assertGreater(task['next_run_at'],self.store.now()+20)
        self.assertIsNone(coordinator.claim(self.run['id'],'workflow',step_id='render'))

    def test_interrupted_control_job_drops_obsolete_runtime_binding_for_takeover(self):
        self.store.execute('UPDATE runs SET sealed=0 WHERE id=?',(self.run['id'],))
        self.f.orchestrator.queue_plan(graph.get(self.store,self.run['id']),{'test_takeover':True})
        run=graph.get(self.store,self.run['id']);job=next(item for item in run['control_jobs'] if item['status']=='ready')
        first=Coordinator(self.store,'old-control-worker','test').claim(run['id'],'workflow',step_id=job['id'])
        self.assertIsNotNone(first);first.release(interrupted=True)
        bound=next(item for item in graph.get(self.store,run['id'])['control_jobs'] if item['id']==job['id'])
        self.assertIn('_environment',bound['bindings'])
        self.f.orchestrator.recover(graph.get(self.store,run['id']))
        recovered=next(item for item in graph.get(self.store,run['id'])['control_jobs'] if item['id']==job['id'])
        self.assertEqual((recovered['status'],recovered['lease_id']),('ready',''))
        self.assertNotIn('_environment',recovered['bindings'])
        successor=Coordinator(self.store,'new-control-worker','new-release').claim(run['id'],'workflow',step_id=job['id'])
        self.assertIsNotNone(successor);successor.release()

    def test_repair_context_requeues_plan_when_old_trigger_is_paused(self):
        self.store.execute("UPDATE run_tasks SET status='paused',error_code='review_incomplete' WHERE run_id=? AND id='render'",(self.run['id'],))
        self.store.execute("UPDATE runs SET sealed=0,replan_after_json='[\"render\"]',repair_context_json=? WHERE id=?",
            (graph.encode({'active':True,'subject_task':'render','source':'review_evidence','error_code':'review_incomplete'}),self.run['id']))
        run=graph.get(self.store,self.run['id'])
        self.f.orchestrator.advance_graph(run,set())
        self.assertTrue(self.store.query("SELECT id FROM planner_runs WHERE run_id=? AND status='pending'",(self.run['id'],)))

    def test_repeated_query_failure_is_bounded_without_a_new_submission(self):
        self.provider_task()
        for expected in ('retry_wait','paused'):
            run=graph.get(self.store,self.run['id']);task=run['tasks'][0]
            self.f.orchestrator.task_failure(run,task,{'error_code':'poll_failed','phase':'poll'})
            updated=graph.get(self.store,run['id'])['tasks'][0]
            self.assertEqual(updated['status'],expected)
            self.assertEqual(updated['external_id'],'original-job')
        self.assertEqual(self.store.query('SELECT COUNT(*) AS n FROM workflow_provider_calls')[0]['n'],1)

    def test_incomplete_review_returns_candidate_to_manager_without_regeneration(self):
        self.store.execute("UPDATE run_tasks SET status='reviewing',phase='validate',candidate_result_json='[\"edit/report.txt\"]',quality_json=? WHERE run_id=?",
                           (graph.encode({'technical_valid':True}),self.run['id']))
        run=graph.get(self.store,self.run['id']);self.f.orchestrator.queue_review(run,'render')
        run=graph.get(self.store,run['id']);job=next(item for item in run['control_jobs'] if item['control_kind']=='REVIEW')
        self.f.orchestrator.task_failure(run,job,{'error_code':'review_incomplete','phase':'local'})
        run=graph.get(self.store,run['id']);task=run['tasks'][0]
        self.assertEqual((task['revision'],task['status'],task['error_code']),(0,'paused','review_incomplete'))
        self.assertEqual(task['candidate_result'],['edit/report.txt'])
        self.assertEqual(task['repair_count'],0)
        self.assertEqual(len(run['tasks']),1)
        self.assertTrue(any(item['control_kind']=='PLAN' and item['status']=='ready' for item in run['control_jobs']))
        self.assertEqual(run['repair_context']['error_code'],'review_incomplete')

    def test_incomplete_review_and_new_input_share_one_safe_point_plan(self):
        import json,uuid
        from webapp import run_inputs
        self.store.execute("UPDATE run_tasks SET status='reviewing',candidate_result_json='[\"edit/report.txt\"]',quality_json=? WHERE run_id=?",
                           (graph.encode({'technical_valid':True}),self.run['id']))
        self.f.orchestrator.queue_review(graph.get(self.store,self.run['id']),'render')
        accepted=run_inputs.accept(self.store,self.run['id'],'owner',{'request_id':uuid.uuid4().hex,'content':'Preserve the report content and gather sufficient evidence'})
        run=graph.get(self.store,self.run['id']);job=next(item for item in run['control_jobs'] if item['control_kind']=='REVIEW')
        self.f.orchestrator.task_failure(run,job,{'error_code':'review_incomplete','phase':'local'})
        self.assertFalse(self.store.query("SELECT id FROM planner_runs WHERE status='pending'"))
        self.f.orchestrator.process_inputs(graph.get(self.store,run['id']))
        plans=self.store.query("SELECT snapshot_json FROM planner_runs WHERE status='pending'")
        self.assertEqual(len(plans),1)
        snapshot=json.loads(plans[0]['snapshot_json'])
        self.assertEqual(snapshot['input_batch']['inputs'][0]['id'],accepted['id'])
        self.assertEqual(snapshot['tasks'][0]['error_code'],'review_incomplete')
