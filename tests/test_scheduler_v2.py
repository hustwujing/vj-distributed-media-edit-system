import unittest
from unittest.mock import patch
from tests import test_workflows as fixtures
local_step=fixtures.local_step
from webapp.task_leases import Coordinator
from webapp.scheduling import candidates
from webapp import agent_store,workflows

class SchedulingTests(unittest.TestCase):
    def setUp(self):
        self.f=fixtures.WorkflowTests();self.f.setUp();self.coordinator=Coordinator(self.f.store,'test','test')
    def tearDown(self):self.f.tearDown()
    def test_orphan_diagnosis_reconciles_after_restart_and_is_idempotent(self):
        step=local_step('failed');step['on_failure']='diagnose'
        self.f.submit([step,local_step('downstream',['failed'])]);w=self.f.get()
        workflows.change(self.f.store,w['id'],'failed',status='paused',error_code='local')
        rows=self.coordinator.candidates()
        self.assertEqual(rows[0]['workflow_status'],'reviewing')
        self.assertEqual(self.f.get()['agent_review']['step'],'failed')
        self.assertEqual(self.f.get()['steps'][1]['status'],'pending')
        for _ in range(3):self.coordinator.candidates()
        self.assertEqual(len(self.f.store.query('SELECT id FROM workflow_agent_reviews')),1)

    def test_orphan_review_reconciliation_respects_user_pause_and_active_lease(self):
        from webapp.workflow_review import reconcile
        step=local_step('failed');step['on_failure']='diagnose'
        self.f.submit([step]);w=self.f.get()
        lease=self.coordinator.claim(self.f.turn,'workflow',step_id='failed')
        workflows.change(self.f.store,w['id'],'failed',status='paused',error_code='local')
        try:self.assertFalse(reconcile(self.f.store,self.f.turn))
        finally:lease.release()
        self.f.store.execute('INSERT INTO workflow_control VALUES (?,1)',(self.f.turn,))
        self.assertFalse(reconcile(self.f.store,self.f.turn))
        self.assertIsNone(self.f.get()['agent_review'])
        workflows.cancel(self.f.store,self.f.turn)
        self.assertFalse(reconcile(self.f.store,self.f.turn))

    def test_readonly_snapshot_without_worker_does_not_read_private_manifest(self):
        from webapp.scheduling import snapshot,limits
        self.f.store.execute('UPDATE execution_workers SET draining=1')
        self.f.store.execute("UPDATE agent_turns SET status='queued' WHERE id=?",(self.f.turn,))
        with patch('webapp.runtime_image.fingerprint',side_effect=PermissionError('private manifest')) as fingerprint:
            value=snapshot(self.f.store,self.f.turn)
            self.assertEqual(value['limits_source'],'defaults')
            self.assertIsNone(value['limits']['runtime_profile'])
            self.assertEqual(value['wait_reason'],'resource')
            fingerprint.assert_not_called()
            with self.assertRaises(PermissionError):limits()

    def test_worker_without_recorded_config_uses_unattested_defaults(self):
        from webapp.scheduling import snapshot
        self.coordinator.heartbeat(False)
        self.f.store.execute('DELETE FROM execution_scheduler_configs')
        with patch('webapp.runtime_image.fingerprint',side_effect=PermissionError('private manifest')):
            value=snapshot(self.f.store,self.f.turn)
        self.assertEqual(value['limits_source'],'defaults')
        self.assertIsNone(value['limits']['runtime_profile'])

    def test_model_missing_still_schedules_local_workflow_and_cancel(self):
        self.f.submit([local_step()])
        self.assertIn(self.f.turn,[r['id'] for r in candidates(self.coordinator,False)])
        self.f.store.execute("UPDATE agent_workflows SET status='resuming'")
        self.f.store.execute("UPDATE agent_turns SET status='queued' WHERE id=?",(self.f.turn,))
        self.assertNotIn(self.f.turn,[r['id'] for r in candidates(self.coordinator,False)])
        workflows.cancel(self.f.store,self.f.turn)
        self.assertNotIn(self.f.turn,[r['id'] for r in candidates(self.coordinator,False)])
        self.assertEqual(self.f.get()['status'],'cancelled')
        self.assertTrue(self.f.store.query('SELECT request_id FROM execution_cancel_requests WHERE task_id=?',(self.f.turn,)))
    def test_manual_wait_does_not_repoll_before_due(self):
        job=self.f.store.add_job(self.f.project,'image',{'kind':'image','image_task':{'action':'generate'}})
        self.f.store.execute("INSERT INTO manual_operation_runs(job_id,state,spec,next_poll,created,updated) VALUES (?,'waiting','{}',?,0,0)",(job['id'],self.f.store.now()+20))
        self.assertNotIn(job['id'],[r['id'] for r in self.coordinator.manual_candidates()])
    def test_manual_cannot_starve_older_unserved_agent(self):
        self.f.store.execute("UPDATE agent_turns SET status='queued' WHERE id=?",(self.f.turn,))
        project=self.f.store.add_project('other','other')['id']
        job=self.f.store.add_job(project,'pack',{'kind':'pack'})
        first=self.coordinator.claim(job['id'],'manual');first.release()
        rows=candidates(self.coordinator,True)
        self.assertEqual(rows[0]['id'],self.f.turn)
    def test_cpu_capacity_enforced_across_execution_classes(self):
        self.f.store.execute("UPDATE agent_turns SET status='queued' WHERE id=?",(self.f.turn,))
        project=self.f.store.add_project('other','other')['id']
        job=self.f.store.add_job(project,'pack',{'kind':'pack'})
        with patch.dict('os.environ',{'VIDEO_USE_NODE_CPU_SLOTS':'2','VIDEO_USE_CONCURRENCY':'4','VIDEO_USE_NODE_CONCURRENCY':'4'}):
            first=self.coordinator.claim(job['id'],'manual')
            self.assertIsNotNone(first)
            self.assertIsNone(self.coordinator.claim(self.f.turn,'agent'))
            first.release()
            self.assertIsNotNone(self.coordinator.claim(self.f.turn,'agent'))

    def test_resource_wait_does_not_consume_manual_recovery_attempts(self):
        from webapp.scheduling import defer_resource
        job=self.f.store.add_job(self.f.project,'pack',{'kind':'pack'})
        lease=self.coordinator.claim(job['id'],'manual')
        defer_resource(lease,10);lease.release()
        self.assertEqual(self.f.store.query('SELECT status FROM execution_attempts WHERE id=?',(lease.attempt,))[0]['status'],'resource_wait')
        self.assertIsNone(self.coordinator.claim(job['id'],'manual'))

    def test_pause_and_resume_do_not_resubmit_external_jobs(self):
        self.f.submit();self.f.tick()
        workflows.pause(self.f.store,self.f.turn)
        self.f.tick();self.assertEqual(self.f.get()['status'],'paused')
        workflows.resume(self.f.store,self.f.turn)
        self.f.tick()
        self.assertEqual(self.f.adapter.calls.count(('submit','generate')),1)

    def test_safe_pause_does_not_need_a_free_compute_slot(self):
        self.f.submit([local_step()])
        project=self.f.store.add_project('busy-owner','busy')['id']
        job=self.f.store.add_job(project,'pack',{'kind':'pack'})
        with patch.dict('os.environ',{'VIDEO_USE_NODE_CONCURRENCY':'1'}):
            busy=self.coordinator.claim(job['id'],'manual')
            try:
                self.assertIsNone(self.coordinator.claim(self.f.turn,'workflow'))
                before=self.f.store.query('SELECT id FROM execution_attempts WHERE run_id=?',(self.f.turn,))
                workflows.pause(self.f.store,self.f.turn)
                self.assertEqual(self.f.get()['status'],'paused')
                self.assertEqual(self.f.store.query('SELECT id FROM execution_attempts WHERE run_id=?',(self.f.turn,)),before)
                self.assertNotIn(self.f.turn,[r['id'] for r in self.coordinator.candidates()])
            finally:busy.release()

    def test_pause_waits_for_own_lease_and_cleanup_and_blocks_stale_claim(self):
        self.f.submit([local_step('a'),local_step('b')])
        lease=self.coordinator.claim(self.f.turn,'workflow',step_id='a')
        self.assertIsNotNone(lease)
        self.f.store.execute("INSERT INTO execution_cleanup(attempt,task_id,node,state,updated) VALUES (?,?,?,'pending',?)",(lease.attempt,self.f.turn,'test',self.f.store.now()))
        workflows.pause(self.f.store,self.f.turn)
        self.assertEqual(self.f.get()['status'],'ready')
        self.assertIsNone(self.coordinator.claim(self.f.turn,'workflow',step_id='b'))
        lease.release()
        self.assertEqual(workflows.settle_pauses(self.f.store),0)
        self.f.store.execute("UPDATE execution_cleanup SET state='complete' WHERE attempt=?",(lease.attempt,))
        self.assertEqual(workflows.settle_pauses(self.f.store),1)
        self.assertEqual(self.f.get()['status'],'paused')
        workflows.resume(self.f.store,self.f.turn)
        next_lease=self.coordinator.claim(self.f.turn,'workflow',step_id='b')
        self.assertIsNotNone(next_lease);next_lease.release()

    def test_pause_retains_pending_review_and_cancellation_still_wins(self):
        self.f.submit([local_step()]);self.f.tick();self.f.tick()
        review=self.f.get()['agent_review']['id']
        workflows.pause(self.f.store,self.f.turn)
        self.assertEqual(self.f.get()['status'],'paused')
        workflows.resume(self.f.store,self.f.turn)
        self.assertEqual(self.f.get()['agent_review']['id'],review)
        self.assertEqual(self.f.get()['steps'][0]['attempts'],1)
        workflows.cancel(self.f.store,self.f.turn)
        self.assertEqual(workflows.settle_pauses(self.f.store),0)
        self.assertEqual(self.f.get()['status'],'cancelled')

    def test_tool_quota_is_not_misreported_as_unknown_submission(self):
        from webapp.task_leases import TaskQuotaReached
        self.f.submit()
        def exhausted(w,s):raise TaskQuotaReached('quota')
        self.f.adapter.submit=exhausted
        self.f.tick()
        self.assertEqual(self.f.get()['steps'][0]['error_code'],'quota')
        self.assertFalse(self.f.store.query('SELECT * FROM workflow_provider_calls'))

    @unittest.skipUnless(__import__('os').geteuid()==0,'Host launcher requires root')
    def test_gpu_unavailable_defers_before_runtime_allocation(self):
        from webapp.agent_host import AgentHost
        from unittest.mock import Mock
        spec={'id':'cutout','name':'cutout','kind':'operation','operation':'image.remove_background',
              'inputs':{'source':{'asset':'asset'}},'goal':'cutout','on_failure':'pause',
              'acceptance':[{'kind':'image','width':8}]}
        self.f.submit([spec])
        host=AgentHost(self.f.store.root)
        lease=host.coordinator.claim(self.f.turn,'workflow')
        allocation=Mock();allocation.acquire.return_value=None
        with patch('webapp.image_sandbox.allocation_for',return_value=allocation),patch('webapp.agent_host.TaskRuntime') as runtime:
            host.execute_claim(lease)
            runtime.assert_not_called()
        self.assertEqual(self.f.get()['steps'][0]['wait_reason'],'resource')
        self.assertEqual(self.f.store.query('SELECT status FROM execution_attempts WHERE id=?',(lease.attempt,))[0]['status'],'resource_wait')

    def test_same_owner_new_manual_jobs_cannot_overtake_old_agent_forever(self):
        self.f.store.execute("UPDATE agent_turns SET status='queued',created=created-60 WHERE id=?",(self.f.turn,))
        project=self.f.store.add_project('owner','another project')['id']
        job=self.f.store.add_job(project,'pack',{'kind':'pack'})
        rows=candidates(self.coordinator,True)
        self.assertEqual(rows[0]['id'],self.f.turn)

    def test_new_project_task_waits_for_previous_cleanup(self):
        self.f.store.execute("UPDATE agent_turns SET status='queued' WHERE id=?",(self.f.turn,))
        old=self.coordinator.claim(self.f.turn,'agent')
        self.f.store.execute("INSERT INTO execution_cleanup(attempt,task_id,node,state,updated) VALUES (?,?,?,'quarantined',?)",(old.attempt,self.f.turn,self.coordinator.node,self.f.store.now()))
        self.f.store.execute("UPDATE agent_turns SET status='cancelled' WHERE id=?",(self.f.turn,))
        old.release()
        new=agent_store.add_turn(self.f.store,self.f.project,'execute','new task',plan_id=self.f.plan)
        self.assertIsNone(self.coordinator.claim(new,'agent'))
        self.assertEqual(self.f.store.query('SELECT reason FROM execution_waits WHERE task_id=?',(new,))[0]['reason'],'cleanup')
        self.f.store.execute("UPDATE execution_cleanup SET state='complete' WHERE attempt=?",(old.attempt,))
        self.assertIsNotNone(self.coordinator.claim(new,'agent'))

    def test_resource_contract_remains_bound_after_step_is_running(self):
        from webapp.scheduling import requirements
        spec={'id':'cutout','name':'cutout','kind':'operation','operation':'image.remove_background',
              'inputs':{'source':{'asset':'asset'}},'goal':'cutout','on_failure':'pause','acceptance':[{'kind':'image','width':8}]}
        self.f.submit([spec])
        lease=self.coordinator.claim(self.f.turn,'workflow')
        self.assertEqual(lease.step_id,'cutout')
        workflows.change(self.f.store,self.f.get()['id'],'cutout',status='running')
        self.assertEqual(requirements(self.f.store,self.f.turn,'workflow',step_id=lease.step_id)['memory_mb'],8192)
        lease.release()
