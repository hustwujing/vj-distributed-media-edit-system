import unittest

from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from webapp.harness_events import AgentEvent
from webapp.runtime_daemon import create_app
from webapp.runtime_protocol import ExecutorProtocol,RuntimeIdentity


class RuntimeDaemonTests(unittest.TestCase):
    def setUp(self):
        self.identity=RuntimeIdentity('attempt-1',2,'sandbox-1')
        self.calls=[]
        def dispatch(method,params):
            self.calls.append((method,params));return {'accepted':True}
        self.app=create_app(self.identity,'secret',dispatch,
            lambda:{'ready':True,'harness':'codex'})
        self.client=TestClient(self.app)

    def test_health_exposes_compatibility_without_control_token(self):
        response=self.client.get('/healthz')
        self.assertEqual(response.status_code,200)
        self.assertEqual(response.json()['harness'],'codex')
        self.assertNotIn('token',response.text)

    def test_websocket_carries_requests_responses_events_and_acks(self):
        executor=ExecutorProtocol(self.identity,'secret')
        with self.client.websocket_connect('/control') as socket:
            socket.send_json(executor.hello());executor.receive(socket.receive_json())
            request=executor.request('runtime.initialize',{'profile':'agent-v1'})
            socket.send_json(request);executor.receive(socket.receive_json())
            self.assertTrue(executor.responses[request['id']]['result']['accepted'])
            self.app.state.runtime_service.publish(
                AgentEvent('message.delta','message-1',{'delta':'hello'}))
            event=executor.receive(socket.receive_json())
            self.assertEqual((event.kind,event.data['delta']),('message.delta','hello'))
            socket.send_json(executor.acknowledge())
        self.assertEqual(self.calls,[('runtime.initialize',{'profile':'agent-v1'})])
        self.assertEqual(self.app.state.runtime_service.protocol.events.acked_through,1)

    def test_unacknowledged_event_replays_on_next_connection(self):
        first=ExecutorProtocol(self.identity,'secret')
        with self.client.websocket_connect('/control') as socket:
            socket.send_json(first.hello());first.receive(socket.receive_json())
            self.app.state.runtime_service.publish(
                AgentEvent('turn.ended',data={'status':'completed'}))
            frame=socket.receive_json()
        second=ExecutorProtocol(self.identity,'secret')
        with self.client.websocket_connect('/control') as socket:
            socket.send_json(second.hello())
            second.receive(socket.receive_json())
            event=second.receive(socket.receive_json())
            self.assertEqual(event.data['status'],'completed')
            socket.send_json(second.acknowledge())
        self.assertEqual(frame['seq'],1)

    def test_invalid_token_closes_without_dispatch(self):
        executor=ExecutorProtocol(self.identity,'wrong')
        with self.client.websocket_connect('/control') as socket:
            socket.send_json(executor.hello())
            with self.assertRaises(WebSocketDisconnect) as closed:socket.receive_json()
        self.assertEqual(closed.exception.code,4403)
        self.assertEqual(self.calls,[])


if __name__=='__main__':unittest.main()
