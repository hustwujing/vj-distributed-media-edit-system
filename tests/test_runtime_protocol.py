import unittest
import threading

from webapp.harness_events import AgentEvent
from webapp.runtime_protocol import (
    AuthenticationError,
    BackpressureExceeded,
    DaemonProtocol,
    ExecutorProtocol,
    ProtocolError,
    ReplayUnavailable,
    RuntimeIdentity,
    decode_frame,
    encode_frame,
)


class RuntimeProtocolTests(unittest.TestCase):
    def setUp(self):
        self.identity=RuntimeIdentity('attempt-1',2,'sandbox-1')
        self.daemon=DaemonProtocol(self.identity,'secret',max_events=3,max_bytes=4096)
        self.executor=ExecutorProtocol(self.identity,'secret')

    def connect(self):
        frames=self.daemon.receive(self.executor.hello(),lambda *_:None)
        for frame in frames:self.executor.receive(frame)
        self.assertTrue(self.executor.connected)

    def test_handshake_fences_token_attempt_generation_and_sandbox(self):
        wrong=ExecutorProtocol(self.identity,'wrong')
        with self.assertRaises(AuthenticationError):
            self.daemon.receive(wrong.hello(),lambda *_:None)
        foreign=ExecutorProtocol(RuntimeIdentity('attempt-1',3,'sandbox-1'),'secret')
        with self.assertRaises(AuthenticationError):
            self.daemon.receive(foreign.hello(),lambda *_:None)
        with self.assertRaises(AuthenticationError):
            self.daemon.receive(self.executor.request('runtime.initialize',{}),lambda *_:None)

    def test_request_response_is_idempotent_and_rejects_id_collision(self):
        self.connect();calls=[]
        request=self.executor.request('session.open',{'session_id':None})
        dispatch=lambda method,params:calls.append((method,params)) or {'session_id':'s1'}
        first=self.daemon.receive(request,dispatch)[0]
        second=self.daemon.receive(request,dispatch)[0]
        self.assertEqual(first,second);self.assertEqual(len(calls),1)
        self.executor.receive(first)
        self.assertEqual(self.executor.responses[request['id']]['result']['session_id'],'s1')
        with self.assertRaisesRegex(ProtocolError,'reused'):
            self.daemon.receive({**request,'method':'turn.start'},dispatch)

    def test_dispatch_failure_is_a_bounded_response(self):
        self.connect();request=self.executor.request('turn.start',{})
        response=self.daemon.receive(request,lambda *_:(_ for _ in ()).throw(RuntimeError('private failure')))[0]
        self.assertEqual(response['error']['code'],'runtime_request_failed')
        self.assertIn('RuntimeError',response['error']['message'])
        self.assertNotIn('private failure',response['error']['message'])

    def test_unacknowledged_events_replay_after_disconnect(self):
        self.connect()
        frames=[self.daemon.publish(AgentEvent('message.delta','m',{'delta':text}))
                for text in ('one','two')]
        self.assertEqual(self.executor.receive(frames[0]).data['delta'],'one')
        self.daemon.disconnect()
        replay=self.daemon.receive(self.executor.hello(),lambda *_:None)
        self.assertEqual([frame.get('seq') for frame in replay[1:]],[2])
        self.executor.receive(replay[0])
        self.assertEqual(self.executor.receive(replay[1]).data['delta'],'two')
        self.daemon.receive(self.executor.acknowledge(),lambda *_:None)
        self.assertEqual(self.daemon.events.acked_through,2)

    def test_acknowledged_events_are_not_replayable(self):
        self.connect();frame=self.daemon.publish(AgentEvent('turn.ended',data={'status':'completed'}))
        self.executor.receive(frame)
        self.daemon.receive(self.executor.acknowledge(),lambda *_:None)
        self.daemon.disconnect();self.executor.last_seq=0
        with self.assertRaises(ReplayUnavailable):
            self.daemon.receive(self.executor.hello(),lambda *_:None)

    def test_event_window_applies_backpressure_without_dropping_terminal_events(self):
        self.connect()
        for index in range(3):self.daemon.publish(AgentEvent('message.delta',str(index),{'delta':'x'}))
        with self.assertRaises(BackpressureExceeded):
            self.daemon.publish(AgentEvent('turn.ended',data={'status':'completed'}))
        self.assertEqual(len(self.daemon.events.frames),3)

    def test_background_publishers_receive_unique_ordered_sequences(self):
        daemon=DaemonProtocol(self.identity,'secret',max_events=64,max_bytes=65536)
        barrier=threading.Barrier(16);frames=[];lock=threading.Lock()
        def publish(index):
            barrier.wait();frame=daemon.publish(AgentEvent('message.delta',str(index),{'delta':'x'}))
            with lock:frames.append(frame)
        threads=[threading.Thread(target=publish,args=(index,)) for index in range(16)]
        for thread in threads:thread.start()
        for thread in threads:thread.join()
        self.assertEqual(sorted(frame['seq'] for frame in frames),list(range(1,17)))

    def test_executor_rejects_gaps_and_foreign_frames(self):
        self.connect()
        one=self.daemon.publish(AgentEvent('message.delta','m',{'delta':'one'}))
        two=self.daemon.publish(AgentEvent('message.delta','m',{'delta':'two'}))
        with self.assertRaisesRegex(ProtocolError,'gap'):self.executor.receive(two)
        with self.assertRaises(AuthenticationError):self.executor.receive({**one,'sandbox_id':'foreign'})

    def test_codec_rejects_oversized_and_non_object_frames(self):
        self.assertEqual(decode_frame(encode_frame({'type':'test'})),{'type':'test'})
        with self.assertRaises(ProtocolError):decode_frame('[]')
        with self.assertRaises(ProtocolError):encode_frame({'value':'x'*(1024*1024)})


if __name__=='__main__':unittest.main()
