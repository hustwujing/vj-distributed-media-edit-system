import hashlib
import unittest
from types import SimpleNamespace

from tests import test_workflows as fixtures
from webapp import agent_store
from webapp.agent_host import AgentHost
from webapp.task_leases import Coordinator


class Runtime:
    is_remote=True
    def __init__(self):self.handler=None;self.events=[];self.session='remote-session';self.opened_with=None
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
    def _release(self):
        if self.store.query('SELECT active FROM execution_leases WHERE attempt=?',(self.lease.attempt,))[0]['active']:
            self.lease.release()

    def test_agent_host_remote_branch_streams_commits_checkpoint_and_finishes(self):
        host=AgentHost.__new__(AgentHost);host.store=self.store;host.redact=lambda value:value
        host.stop=__import__('threading').Event()
        host.notify=lambda task,kind,payload:agent_store.event(self.store,task,kind,payload)
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
        host=AgentHost.__new__(AgentHost);host.store=self.store;host.redact=lambda value:value
        host.stop=__import__('threading').Event()
        host.notify=lambda task,kind,payload:agent_store.event(self.store,task,kind,payload)
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


if __name__=='__main__':unittest.main()
