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
        self.assertEqual((task['status'],task['recovery_count']),('waiting',2))
        self.assertEqual(len(self.store.query('SELECT request_key FROM workflow_provider_calls')),1)

    def test_repeated_wait_receipts_back_off_without_spending_recovery_budget(self):
        self.provider_task()
        self.store.execute("UPDATE run_tasks SET status='waiting',lease_id='',next_run_at=0,recovery_count=0 WHERE run_id=?",(self.run['id'],))
        for index,expected in enumerate((2,10,20,40,60)):
            self.store.execute('UPDATE run_tasks SET next_run_at=0 WHERE run_id=?',(self.run['id'],))
            self.f.orchestrator.advance_ready(graph.get(self.store,self.run['id']))
            lease=Coordinator(self.store,'poll-backoff','test').claim(self.run['id'],'workflow',step_id='render')
            self.assertIsNotNone(lease)
            try:
                with lease.bind():stage_result(self.store,lease,'waiting',{'external_id':'original-job','poll_after':2 if index==0 else 5})
            finally:lease.release()
            before=self.store.now()
            self.f.orchestrator.accept_results(graph.get(self.store,self.run['id']))
            task=graph.get(self.store,self.run['id'])['tasks'][0]
            self.assertAlmostEqual(task['next_run_at']-before,expected,delta=.5)
            self.assertEqual(task['poll_count'],index+1)
            self.assertEqual(task['recovery_count'],0)
            self.assertEqual(len(self.store.query('SELECT request_key FROM workflow_provider_calls')),1)
        self.f.orchestrator.task_failure(graph.get(self.store,self.run['id']),task,{'error_code':'poll_failed','phase':'poll'})
        task=graph.get(self.store,self.run['id'])['tasks'][0]
        self.assertEqual((task['status'],task['recovery_count']),('retry_wait',1))

    def timed_external_wait(self):
        self.provider_task()
        self.store.execute("UPDATE run_tasks SET status='waiting',lease_id='',wait_recorded_at=?,next_run_at=? WHERE run_id=?",
            (self.store.now()-8000,self.store.now()+60,self.run['id']))
        self.store.execute('UPDATE workflow_provider_calls SET implementation=? WHERE workflow=?',
            (graph.encode({'budget_group':'video.generate_from_image'}),self.run['id']))
        run=self.f.tick()
        self.assertEqual((run['status'],run['terminal_reason']),('WAITING_USER','external_wait_timeout'))
        return self.store.query("SELECT * FROM workflow_decisions WHERE run_id=? AND kind='external_wait' AND status='pending'",(self.run['id'],))[0]

    def test_wait_timeout_answer_is_processed_before_timeout_check(self):
        decision=self.timed_external_wait()
        command=graph.command(self.store,self.run['id'],'owner','answer',
            {'decision_id':decision['id'],'snapshot_digest':decision['snapshot_digest'],'action':'approve'})
        run=self.f.tick()
        self.assertEqual(self.store.query('SELECT status FROM workflow_commands WHERE id=?',(command['id'],))[0]['status'],'applied')
        self.assertEqual(run['status'],'ACTIVE')
        self.assertGreater(run['tasks'][0]['wait_recorded_at'],self.store.now()-5)
        self.assertFalse(self.store.query("SELECT id FROM workflow_decisions WHERE run_id=? AND status='pending'",(self.run['id'],)))
        self.assertEqual(run['reconcile_owner'],'')

    def test_wait_timeout_does_not_starve_cancel(self):
        self.timed_external_wait()
        command=graph.command(self.store,self.run['id'],'owner','cancel')
        run=self.f.tick()
        self.assertEqual(self.store.query('SELECT status FROM workflow_commands WHERE id=?',(command['id'],))[0]['status'],'applied')
        self.assertIn(run['status'],('CANCELLING','CANCELLED'))
        self.assertFalse(self.store.query("SELECT id FROM workflow_decisions WHERE run_id=? AND status='pending'",(self.run['id'],)))

    def test_abandon_external_wait_cleans_only_selected_task(self):
        decision=self.timed_external_wait()
        now=self.store.now()
        for task_id in ('render','sibling'):
            attempt=task_id+'-attempt'
            self.store.execute("INSERT INTO execution_attempts(id,run_id,worker,generation,kind,started,status,step_id) VALUES (?,?,?,1,'workflow',?,'finished',?)",
                (attempt,self.run['id'],'worker',now,task_id))
            self.store.execute("INSERT INTO execution_cleanup(attempt,task_id,node,state,updated) VALUES (?,?,?,'allocated',?)",
                (attempt,self.run['id'],'worker',now))
        self.store.execute("INSERT INTO workflow_provider_calls(request_key,workflow,step,revision,attempt,provider,operation,state,external_id,inputs,created,updated,implementation) VALUES (?,?,?,0,1,?,'video.generate_from_image','running','sibling-job','{}',?,?,?)",
            (self.run['id']+':sibling:0',self.run['id'],'sibling','https://provider.invalid',now,now,graph.encode({'budget_group':'video.generate_from_image'})))
        command=graph.command(self.store,self.run['id'],'owner','answer',
            {'decision_id':decision['id'],'snapshot_digest':decision['snapshot_digest'],'action':'reject'})
        self.f.tick()
        self.assertEqual(self.store.query('SELECT status FROM workflow_commands WHERE id=?',(command['id'],))[0]['status'],'applied')
        cleanup={row['attempt']:row['state'] for row in self.store.query('SELECT attempt,state FROM execution_cleanup')}
        self.assertEqual(cleanup,{'render-attempt':'pending','sibling-attempt':'allocated'})
        self.assertEqual([row['id'] for row in self.store.query('SELECT id FROM execution_external_cleanup')],[self.run['id']+':render:0'])

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
        from webapp.workflow_api import public
        usage=public(self.store,graph.get(self.store,self.run['id']))['token_usage']
        self.assertEqual((usage['settled_input'],usage['fully_unknown_reserved'],usage['unknown_output_reserved']),(0,9000,0))

    def test_expired_running_attempt_is_visible_then_reconciled_while_user_waits(self):
        import os,uuid
        from unittest.mock import patch
        from webapp.observation_collector import Archive,sample
        from webapp.task_leases import LeaseLost
        coordinator=Coordinator(self.store,'killed-worker','test')
        lease=coordinator.claim(self.run['id'],'workflow',step_id='render');self.assertIsNotNone(lease)
        lease.start_compute()
        reserved_seconds=self.store.query('SELECT reserved FROM execution_time_slices WHERE attempt=?',(lease.attempt,))[0]['reserved']
        used_before=self.store.query('SELECT used_seconds FROM execution_limits WHERE task_id=?',(self.run['id'],))[0]['used_seconds']
        self.store.execute("INSERT INTO workflow_model_reservations(id,run_id,attempt_id,reserved_tokens,state,created_at,input_reserved_tokens,output_reserved_tokens) VALUES (?,?,?,?,?,?,?,?)",
            (uuid.uuid4().hex,self.run['id'],lease.attempt,6144,'reserved',self.store.now(),2048,4096))
        archive=Archive(self.store.root/'orphan-observation')
        def gauges():
            with patch.dict(os.environ,{'VIDEO_USE_OBSERVATION_API_PORTS':''}), \
                 patch('webapp.observation_collector.storage_health',return_value='available'), \
                 patch('webapp.observation_collector.gpu_health',return_value={}):
                return sample(self.store,archive)['gauges']
        self.f.orchestrator.reconcile_model_reservations(graph.get(self.store,self.run['id']))
        self.assertEqual(self.store.query('SELECT state FROM workflow_model_reservations')[0]['state'],'reserved')
        self.assertEqual(gauges()['model_orphan_reservations'],0)
        # No release(): reproduce the durable state left by SIGKILL.
        self.store.execute('UPDATE execution_leases SET expires=0 WHERE attempt=?',(lease.attempt,))
        self.store.execute("UPDATE run_tasks SET status='paused' WHERE run_id=? AND id='render'",(self.run['id'],))
        self.store.execute("UPDATE runs SET status='WAITING_USER' WHERE id=?",(self.run['id'],))
        before=gauges();self.assertEqual((before['model_orphan_reservations'],before['model_orphan_reserved_tokens']),(1,6144))
        for _ in range(3):self.f.orchestrator.reconcile_model_reservations(graph.get(self.store,self.run['id']))
        row=self.store.query('SELECT state,used_tokens FROM workflow_model_reservations')[0]
        self.assertEqual(row,{'state':'interrupted_usage_unknown','used_tokens':6144})
        self.assertEqual(self.store.query('SELECT active FROM execution_leases WHERE attempt=?',(lease.attempt,))[0]['active'],0)
        self.assertEqual(self.store.query('SELECT status FROM execution_attempts WHERE id=?',(lease.attempt,))[0]['status'],'expired')
        self.assertEqual(self.store.query('SELECT computing,reserved FROM execution_time_slices WHERE attempt=?',(lease.attempt,))[0],{'computing':0,'reserved':0.0})
        used_after=self.store.query('SELECT used_seconds FROM execution_limits WHERE task_id=?',(self.run['id'],))[0]['used_seconds']
        self.assertAlmostEqual(used_after-used_before,reserved_seconds)
        self.assertEqual(gauges()['model_orphan_reservations'],0)
        with self.assertRaises(LeaseLost):lease.renew()

    def test_heartbeat_expiry_allows_recovery_and_new_generation_claim(self):
        coordinator=Coordinator(self.store,'heartbeat-recovery','test')
        old=coordinator.claim(self.run['id'],'workflow',step_id='render');self.assertIsNotNone(old)
        old.start_compute()
        self.store.execute('UPDATE execution_leases SET expires=0 WHERE attempt=?',(old.attempt,))
        coordinator.heartbeat()
        self.assertEqual(self.store.query('SELECT status FROM execution_attempts WHERE id=?',(old.attempt,))[0]['status'],'expired')
        self.f.orchestrator.recover(graph.get(self.store,self.run['id']))
        replacement=coordinator.claim(self.run['id'],'workflow',step_id='render')
        self.assertIsNotNone(replacement)
        try:self.assertGreater(replacement.generation,old.generation)
        finally:replacement.release()

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
