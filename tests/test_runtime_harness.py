import json
import queue
import unittest
from types import SimpleNamespace

from webapp.harness_adapters import HarnessAdapter
from webapp.runtime_harness import HarnessController,HarnessStateError,StdioHarnessBridge


class QueueReader:
    def __init__(self):self.items=queue.Queue();self.closed=False
    def readline(self,_limit=-1):return self.items.get(timeout=2)
    def push(self,value):self.items.put(value)
    def close(self):
        if not self.closed:self.closed=True;self.items.put('')


class HarnessWriter:
    def __init__(self,output):self.output=output;self.buffer='';self.closed=False
    def write(self,value):
        self.buffer+=value
        while '\n' in self.buffer:
            line,self.buffer=self.buffer.split('\n',1);message=json.loads(line)
            if 'id' not in message:continue
            method=message['method']
            result=({'name':'test'} if method=='initialize' else
                    {'session_id':'session-1'} if method=='session/open' else
                    {'started':True} if method=='turn/start' else {'status':'interrupted'})
            if method=='turn/start':
                self.output.push(json.dumps({'method':'agent/event','params':{
                    'kind':'turn.ended','id':'turn-1','data':{'status':'completed'}}})+'\n')
            self.output.push(json.dumps({'id':message['id'],'result':result})+'\n')
        return len(value)
    def flush(self):pass
    def close(self):self.closed=True


class FakeProcess:
    pid=12345
    def __init__(self):
        self.stdout=QueueReader();self.stderr=QueueReader();self.stdin=HarnessWriter(self.stdout);self.returncode=None
    def poll(self):return self.returncode
    def wait(self,timeout=None):self.returncode=0;return 0


class RuntimeHarnessTests(unittest.TestCase):
    def setUp(self):
        self.process=FakeProcess();self.events=[]
        adapter=HarnessAdapter();adapter.id='test'
        self.bridge=StdioHarnessBridge(adapter,['/opt/test-harness'],
            process_factory=lambda *args,**kwargs:self.process)
        self.controller=HarnessController(adapter,SimpleNamespace(model='m',reasoning_effort='high'),
            self.bridge,self.events.append,{'profile':'profile-1','skill':'skill-1'})
        self.addCleanup(self._close)
    def _close(self):
        # Fake processes are not real process groups; mark them exited first.
        self.process.returncode=0;self.controller.close()

    def test_full_runtime_session_and_streamed_terminal_event(self):
        initialized=self.controller.dispatch('runtime.initialize',{'profile':'profile-1','skill':'skill-1'})
        self.assertEqual(initialized['harness'],'test')
        opened=self.controller.dispatch('session.open',{'instructions':'trusted'})
        self.assertEqual(opened['session_id'],'session-1')
        result=self.controller.dispatch('turn.start',{'session_id':'session-1','text':'do work'})
        self.assertTrue(result['started'])
        for _ in range(100):
            if self.events:break
            __import__('time').sleep(.001)
        self.assertEqual(self.events[0].kind,'turn.ended')
        self.assertEqual(self.controller.state,'session')
        self.assertEqual(self.controller.dispatch('session.export',{}),
            {'session_id':'session-1','native_state':'owned_by_harness'})

    def test_frozen_profile_and_lifecycle_are_enforced(self):
        with self.assertRaisesRegex(HarnessStateError,'frozen'):
            self.controller.dispatch('runtime.initialize',{'profile':'other'})
        with self.assertRaisesRegex(HarnessStateError,'not initialized'):
            self.controller.dispatch('session.open',{'instructions':'x'})
        self.controller.dispatch('runtime.initialize',{'profile':'profile-1','skill':'skill-1'})
        self.controller.dispatch('session.open',{'instructions':'x'})
        with self.assertRaisesRegex(HarnessStateError,'does not match'):
            self.controller.dispatch('turn.start',{'session_id':'foreign','text':'x'})

    def test_input_limits_are_checked_before_native_rpc(self):
        self.controller.dispatch('runtime.initialize',{'profile':'profile-1','skill':'skill-1'})
        with self.assertRaisesRegex(ValueError,'instructions'):
            self.controller.dispatch('session.open',{'instructions':'x'*(256*1024+1)})
        self.controller.dispatch('session.open',{'instructions':'ok'})
        with self.assertRaisesRegex(ValueError,'turn input'):
            self.controller.dispatch('turn.start',{'session_id':'session-1','text':'x'*(1024*1024+1)})


if __name__=='__main__':unittest.main()
