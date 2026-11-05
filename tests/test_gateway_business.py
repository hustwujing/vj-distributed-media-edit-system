import unittest
from unittest.mock import Mock,patch
import threading

from webapp.gateway_business import GatewayBusinessCore
from webapp.gateway_contracts import GatewayResult


class GatewayBusinessTests(unittest.TestCase):
    def test_frozen_protocol_exposes_only_matching_model_routes(self):
        for protocol,expected in [('responses',{'/responses','/v1/responses'}),
                                  ('anthropic',{'/messages','/v1/messages'}),
                                  ('chat_completions',{'/chat/completions','/v1/chat/completions'})]:
            gateway=Mock();gateway.model_adapter.protocol=protocol
            self.assertEqual(set(GatewayBusinessCore(gateway).handlers()),expected)

    def test_model_handler_reuses_gateway_result_and_rejects_invalid_json(self):
        gateway=Mock();gateway.model_adapter.protocol='responses'
        expected=GatewayResult(200,{},[b'ok'],lambda:None);gateway.forward_result.return_value=expected
        core=GatewayBusinessCore(gateway)
        self.assertIs(core.handlers()['/v1/responses'](b'{"input":[]}',Mock()),expected)
        gateway.forward_result.assert_called_once_with({'input':[]})
        for body in (b'{',b'[]'):
            with self.assertRaises(ValueError):core.model(body,Mock())

    def test_shared_model_result_owns_provider_and_usage_stream_lifetime(self):
        from webapp.agent_gateway import Gateway
        from webapp.agent_profiles import AgentProfile
        from webapp.model_adapters import ResponsesAdapter
        gateway=object.__new__(Gateway);gateway.check=Mock();gateway.dynamic_task=False
        gateway.profile=AgentProfile();gateway.model_adapter=ResponsesAdapter();gateway.broker=None
        gateway.lease=None;gateway.lock=threading.Lock();gateway.calls=0
        gateway.model='model';gateway.base_url='https://provider.invalid';gateway.key='test-key'
        frame=(b'data: {"type":"response.completed","response":{"id":"r1",'
               b'"usage":{"input_tokens":1,"output_tokens":2,"total_tokens":3}}}\n\n')
        response=Mock();response.status_code=200;response.headers={'Content-Type':'text/event-stream'}
        response.raw=None;response.iter_content.return_value=iter((frame,))
        response.__enter__=Mock(return_value=response);response.__exit__=Mock(return_value=False)
        client=Mock();client.post.return_value=response
        client.__enter__=Mock(return_value=client);client.__exit__=Mock(return_value=False)
        with patch('webapp.egress_policy.session',return_value=client):
            result=gateway.forward_result({'input':[]})
            response.__exit__.assert_not_called()
            self.assertEqual(b''.join(result.chunks),frame)
            response.__exit__.assert_called_once()
        self.assertEqual(gateway.check.call_count,2)

    def test_node_authorization_and_business_adapter_share_one_result_contract(self):
        from webapp.node_gateway import GrantAuthority,InMemoryGrantStore,NodeGatewayAuthorizer,NodeGatewayCore
        store=InMemoryGrantStore(lambda:100);issued=GrantAuthority(store).issue(
            task_id='task',attempt_id='attempt',generation=1,sandbox_id='sandbox',
            source_ip='172.30.0.8',routes={'/v1/responses'},expires_at=200,
            max_calls=1,max_request_bytes=100,max_response_bytes=100)
        gateway=Mock();gateway.model_adapter.protocol='responses'
        closed=[];gateway.forward_result.return_value=GatewayResult(200,{},[b'ok'],lambda:closed.append(True))
        core=NodeGatewayCore(NodeGatewayAuthorizer(store),GatewayBusinessCore(gateway).handlers())
        headers={'Authorization':'Bearer '+issued.token,'X-Video-Use-Attempt':'attempt',
            'X-Video-Use-Generation':'1','X-Video-Use-Sandbox':'sandbox'}
        result=core.dispatch(headers,'/v1/responses','172.30.0.8',b'{"input":[]}')
        self.assertEqual(b''.join(result),b'ok');self.assertEqual(closed,[True])


if __name__=='__main__':unittest.main()
