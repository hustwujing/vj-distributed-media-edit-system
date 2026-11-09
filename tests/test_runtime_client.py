import unittest

from webapp.harness_events import AgentEvent
from webapp.runtime_client import RemoteRuntimeError,RuntimeClient,RuntimeTransportError
from webapp.runtime_protocol import DaemonProtocol,RuntimeIdentity


class InMemoryServer:
    def __init__(self,identity):
        self.calls=[];self.daemon=DaemonProtocol(identity,'secret');self.active=None
        self.fail_after_request=False
    def dispatch(self,method,params):
        self.calls.append((method,params))
        if method=='turn.interrupt':raise ValueError('not running')
        return {'accepted':True}
    def connect(self):
        self.active=InMemoryConnection(self);return self.active
    def emit(self,event):
        frame=self.daemon.publish(event)
        if self.active:self.active.incoming.append(frame)


class InMemoryConnection:
    def __init__(self,server):self.server=server;self.incoming=[];self.closed=False
    def send(self,frame):
        responses=self.server.daemon.receive(frame,self.server.dispatch)
        if frame.get('type')=='request' and self.server.fail_after_request:
            self.server.fail_after_request=False;self.server.daemon.disconnect()
            raise RuntimeTransportError('lost response')
        self.incoming.extend(responses)
    def receive(self,_timeout):
        if not self.incoming:raise RuntimeTransportError('no frame')
        return self.incoming.pop(0)
    def close(self):
        if not self.closed:self.closed=True;self.server.daemon.disconnect()


class RuntimeClientTests(unittest.TestCase):
    def test_missing_dependency_is_not_a_retryable_network_error(self):
        from unittest.mock import patch
        from webapp.runtime_client import WebSocketTransport,RuntimeDependencyError
        from webapp.workflow_executor import transient_model_error
        with patch.dict('sys.modules',{'websockets.sync.client':None}):
            with self.assertRaises(RuntimeDependencyError) as raised:WebSocketTransport('ws://127.0.0.1:1')
        self.assertFalse(transient_model_error(raised.exception))
        self.assertFalse(transient_model_error(RuntimeTransportError('control channel unavailable')))

    def test_real_websocket_dependency_handshake_rpc_and_idle_timeout(self):
        import threading,json
        from websockets.sync.server import serve
        daemon=DaemonProtocol(self.identity,'secret')
        def handler(socket):
            for message in socket:
                for response in daemon.receive(json.loads(message),lambda method,params:{'echo':params}):
                    socket.send(json.dumps(response))
        with serve(handler,'127.0.0.1',0) as server:
            thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
            client=RuntimeClient(f'ws://127.0.0.1:{server.socket.getsockname()[1]}',self.identity,'secret',lambda event:None,timeout=.1)
            try:
                client.connect();self.assertEqual(client.call('runtime.initialize',{'a':1}),{'echo':{'a':1}})
                with self.assertRaises(TimeoutError):client.receive_once()
                self.assertEqual(client.call('runtime.initialize',{'a':2}),{'echo':{'a':2}})
            finally:client.close();server.shutdown();thread.join(timeout=2)

    def setUp(self):
        self.identity=RuntimeIdentity('attempt-1',1,'sandbox-1')
        self.server=InMemoryServer(self.identity);self.events=[]
        self.client=RuntimeClient('ws://unused',self.identity,'secret',self.events.append,
            connector=self.server.connect)
        self.addCleanup(self.client.close)

    def test_call_and_event_acknowledgement(self):
        self.client.connect()
        self.server.emit(AgentEvent('message.delta','m',{'delta':'hello'}))
        self.client.receive_once()
        self.assertEqual(self.events[0].data['delta'],'hello')
        self.assertEqual(self.server.daemon.events.acked_through,1)
        self.assertTrue(self.client.call('runtime.initialize',{})['accepted'])

    def test_ambiguous_request_reconnects_with_same_id_without_redispatch(self):
        self.server.fail_after_request=True
        result=self.client.call('runtime.initialize',{'profile':'p'})
        self.assertTrue(result['accepted'])
        self.assertEqual(self.server.calls,[('runtime.initialize',{'profile':'p'})])

    def test_event_callback_failure_does_not_ack(self):
        client=RuntimeClient('ws://unused',self.identity,'secret',
            lambda event:(_ for _ in ()).throw(RuntimeError('durability failed')),
            connector=self.server.connect)
        self.addCleanup(client.close);client.connect()
        self.server.emit(AgentEvent('turn.ended',data={'status':'completed'}))
        with self.assertRaisesRegex(RuntimeError,'durability failed'):client.receive_once()
        self.assertEqual(self.server.daemon.events.acked_through,0)
        self.assertEqual(client.protocol.last_seq,0)

    def test_remote_error_is_structured(self):
        with self.assertRaises(RemoteRuntimeError) as failure:
            self.client.call('turn.interrupt',{})
        self.assertEqual(failure.exception.error['code'],'runtime_request_failed')


if __name__=='__main__':unittest.main()
