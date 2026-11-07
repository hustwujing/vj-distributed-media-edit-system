import hashlib
import unittest
import threading
from types import SimpleNamespace
from unittest.mock import Mock,patch

from tests import test_workflows as fixtures
from webapp import agent_store
from webapp.agent_host import AgentHost,Shutdown,Cancelled
from webapp.task_leases import Coordinator,LeaseLost
from webapp.task_lifecycle import TaskLimitReached


class Runtime:
    is_remote=True
    def __init__(self):
        self.handler=None;self.events=[];self.session='remote-session';self.opened_with=None
        self.cleaning=False;self.interrupted=False
    def begin_cleanup(self):self.cleaning=True
    def interrupt(self):self.interrupted=True
    def revoked(self):return False
    def set_event_handler(self,handler):self.handler=handler
    def open_session(self,instructions,session_id=None):
        self.opened_with=session_id;return {'session_id':session_id or self.session}
    def run_turn(self,session_id,text):
        from webapp.harness_events import AgentEvent
        self.events=[AgentEvent('message.completed','m',{'text':'remote complete','phase':'final_answer'}),
                     AgentEvent('turn.ended','t',{'status':'completed'})]
        return {'started':True}
    def receive_once(self):self.handler(self.events.pop(0))
    def export_session(self):
        def record(data):return {'size':len(data),'sha256':hashlib.sha256(data).hexdigest(),'executable':False}
        return {'session_id':self.session,'manifest':{
            'workspace':{'edit/output.txt':record(b'output')},
            'state':{'session.bin':record(b'session')}}}
    def collect(self,manifest,sink):
        values={('workspace','edit/output.txt'):b'output',('state','session.bin'):b'session'}
        for group,records in manifest.items():
            for name,record in records.items():sink(group,name,values[(group,name)],record)


class RemoteAgentTests(unittest.TestCase):
    def setUp(self):
        self.f=fixtures.WorkflowTests();self.f.setUp();self.addCleanup(self.f.tearDown)
        self.store=self.f.store;self.task=self.f.turn
        self.store.execute("UPDATE agent_turns SET status='queued' WHERE id=?",(self.task,))
        self.coordinator=Coordinator(self.store,'remote-node','test')
        self.lease=self.coordinator.claim(self.task,'agent');self.addCleanup(self._release)
        self.runtime=Runtime();self.lease.runtime=self.runtime
        self.lease.backend_binding=SimpleNamespace(runtime_profile='agent-v1')
        self.store.execute("UPDATE agent_turns SET status='running' WHERE id=?",(self.task,))
        self.host=AgentHost.__new__(AgentHost);self.host.store=self.store;self.host.redact=lambda value:value
        self.host.stop=threading.Event();self.host.lock=threading.Lock();self.host.active={}
        self.host.notify=lambda task,kind,payload:agent_store.event(self.store,task,kind,payload)
    def _release(self):
        if self.store.query('SELECT active FROM execution_leases WHERE attempt=?',(self.lease.attempt,))[0]['active']:
            self.lease.release()

    def test_agent_host_remote_branch_streams_commits_checkpoint_and_finishes(self):
        host=self.host
        with self.lease.bind():result=AgentHost.run_turn(host,self.task)
        self.assertEqual(result.text,'remote complete')
        row=agent_store.turn(self.store,self.task);self.assertEqual(row['status'],'succeeded')
        self.assertEqual(row['artifacts'][0]['name'],'output.txt')
        checkpoint=self.store.query('SELECT attempt,manifest FROM execution_checkpoints WHERE project=?',(self.f.project,))[0]
        self.assertEqual(checkpoint['attempt'],self.lease.attempt)
        self.assertIn('session.bin',checkpoint['manifest'])
        session=self.store.query('SELECT thread_id FROM agent_sessions WHERE project=?',(self.f.project,))[0]
        self.assertEqual(session['thread_id'],'remote-session')
        self.assertIsNone(self.runtime.opened_with)

    def test_next_attempt_restores_session_only_for_same_remote_profile(self):
        host=self.host
        with self.lease.bind():AgentHost.run_turn(host,self.task)
        self.lease.release()
        next_task=agent_store.add_turn(self.store,self.f.project,'execute','continue')
        self.store.execute("UPDATE agent_turns SET status='queued' WHERE id=?",(next_task,))
        lease=self.coordinator.claim(next_task,'agent');self.assertIsNotNone(lease)
        runtime=Runtime();lease.runtime=runtime;lease.backend_binding=SimpleNamespace(runtime_profile='agent-v1')
        self.store.execute("UPDATE agent_turns SET status='running' WHERE id=?",(next_task,))
        try:
            with lease.bind():AgentHost.run_turn(host,next_task)
            self.assertEqual(runtime.opened_with,'remote-session')
        finally:lease.release()

    def assert_failed_without_redispatch(self,error):
        row=agent_store.turn(self.store,self.task)
        self.assertEqual(row['status'],'failed');self.assertIn(error,row['error'])
        self.assertTrue(self.runtime.cleaning)
        self.lease.release()
        self.assertIsNone(self.coordinator.claim(self.task,'agent'))

    def test_failed_harness_ends_turn_and_preserves_partial_public_text(self):
        from webapp.harness_events import AgentEvent
        def run(*args):
            self.runtime.events=[AgentEvent('message.delta','partial',{'delta':'partial output'}),
                                 AgentEvent('turn.ended','t',{'status':'failed','error':'harness crashed'})]
        self.runtime.run_turn=run
        with self.lease.bind():self.host.run_turn(self.task)
        self.assertEqual(agent_store.turn(self.store,self.task)['text'],'partial output')
        self.assert_failed_without_redispatch('harness crashed')

    def test_timeout_ends_turn_without_retry(self):
        clock=Mock();clock.monotonic.return_value=10
        def receive():clock.monotonic.return_value=100000
        self.runtime.receive_once=receive
        with self.lease.bind(),patch('webapp.remote_agent.time',clock):self.host.run_turn(self.task)
        self.assert_failed_without_redispatch('Agent 运行时限')

    def test_export_failure_ends_turn_without_retry(self):
        self.runtime.export_session=Mock(side_effect=ValueError('export integrity mismatch'))
        with self.lease.bind():self.host.run_turn(self.task)
        self.assertFalse(self.store.query('SELECT * FROM execution_checkpoints'))
        self.assert_failed_without_redispatch('export integrity mismatch')

    def test_collect_failure_aborts_checkpoint_and_ends_turn(self):
        self.runtime.collect=Mock(side_effect=ValueError('manifest integrity mismatch'))
        with self.lease.bind():self.host.run_turn(self.task)
        self.assertFalse(self.store.query('SELECT * FROM execution_checkpoints'))
        self.assertEqual(list((self.store.project_dir(self.f.project)/'execution-checkpoints').iterdir()),[])
        self.assert_failed_without_redispatch('manifest integrity mismatch')

    def test_checkpoint_commit_failure_ends_turn_without_retry(self):
        with self.lease.bind(),patch('webapp.remote_agent.RemoteCheckpointSink.commit',side_effect=ValueError('checkpoint rejected')):
            self.host.run_turn(self.task)
        self.assertFalse(self.store.query('SELECT * FROM execution_checkpoints'))
        self.assert_failed_without_redispatch('checkpoint rejected')

    def test_failed_harness_pauses_uncheckpointed_workflow(self):
        from webapp import workflows
        workflows.submit(self.store,self.task,{'key':'remote-staged','name':'Remote plan','steps':[fixtures.local_step()]})
        self.runtime.open_session=Mock(side_effect=RuntimeError('harness crashed'))
        with self.lease.bind():self.host.run_turn(self.task)
        self.assertEqual(workflows.get(self.store,self.task)['status'],'paused')
        self.assertEqual(agent_store.turn(self.store,self.task)['status'],'paused')
        self.assertFalse(self.store.query('SELECT * FROM execution_checkpoints'))
        self.lease.release()
        self.assertIsNone(self.coordinator.claim(self.task,'agent'))

    def test_shutdown_propagates_for_takeover_without_new_model_turn(self):
        self.host.stop.set();self.runtime.run_turn=Mock()
        with self.lease.bind(),self.assertRaises(Shutdown):self.host.run_turn(self.task)
        self.assertEqual(agent_store.turn(self.store,self.task)['status'],'running')
        self.runtime.run_turn.assert_not_called()
        self.assertTrue(self.runtime.interrupted);self.assertTrue(self.runtime.cleaning)

    def test_lost_lease_and_task_budget_propagate_without_business_completion(self):
        for exc in (LeaseLost('lost'),TaskLimitReached('deadline')):
            with self.subTest(exception=type(exc).__name__):
                with self.lease.bind(),patch.object(self.lease,'check',side_effect=exc),self.assertRaises(type(exc)):
                    self.host.run_turn(self.task)
                self.assertEqual(agent_store.turn(self.store,self.task)['status'],'running')

    def test_explicit_cancel_uses_shared_cancelled_completion(self):
        self.runtime.open_session=Mock(side_effect=Cancelled())
        with self.lease.bind():self.host.run_turn(self.task)
        self.assertEqual(agent_store.turn(self.store,self.task)['status'],'cancelled')
        self.assertTrue(self.runtime.cleaning)


if __name__=='__main__':unittest.main()
