import unittest
from unittest.mock import Mock,patch
import threading
import contextlib
import json

from webapp.gateway_business import GatewayBusinessCore
from webapp.gateway_contracts import GatewayResult


class GatewayBusinessTests(unittest.TestCase):
    def test_frozen_protocol_exposes_only_matching_model_routes(self):
        for protocol,expected in [('responses',{'/responses','/v1/responses'}),
                                  ('anthropic',{'/messages','/v1/messages'}),
                                  ('chat_completions',{'/chat/completions','/v1/chat/completions'})]:
            gateway=Mock();gateway.model_adapter.protocol=protocol;gateway.workflow_callback=None
            routes=set(GatewayBusinessCore(gateway).handlers())
            self.assertTrue(expected<=routes)
            all_model={'/responses','/v1/responses','/messages','/v1/messages',
                       '/chat/completions','/v1/chat/completions'}
            self.assertEqual(routes&all_model,expected)

    def test_model_handler_reuses_gateway_result_and_rejects_invalid_json(self):
        gateway=Mock();gateway.model_adapter.protocol='responses';gateway.workflow_callback=None
        gateway.lease=None;gateway.check=Mock()
        expected=GatewayResult(200,{},[b'ok'],lambda:None);gateway.forward_result.return_value=expected
        core=GatewayBusinessCore(gateway)
        self.assertIs(core.handlers()['/v1/responses'](b'{"input":[]}',Mock()),expected)
        gateway.forward_result.assert_called_once_with({'input':[]})
        for body in (b'{',b'[]'):
            with self.assertRaises(ValueError):core.decode(body)

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

    def test_native_buffered_transport_requests_json_and_returns_sdk_sse(self):
        from webapp.agent_gateway import Gateway
        from webapp.agent_profiles import AgentProfile
        from webapp.model_adapters import ResponsesAdapter,StructuredStream
        gateway=object.__new__(Gateway);gateway.check=Mock();gateway.dynamic_task=False
        gateway.profile=AgentProfile(native_response_transport='buffered');gateway.model_adapter=ResponsesAdapter();gateway.broker=None
        gateway.lease=None;gateway.lock=threading.Lock();gateway.calls=0;gateway.response_transport='buffered'
        gateway.model='model';gateway.base_url='https://provider.invalid';gateway.key='test-key'
        payload={'id':'response-1','status':'completed','model':'model','output':[
            {'type':'function_call','name':'finish','arguments':'{"ok":true}'}],
            'usage':{'input_tokens':2,'output_tokens':1,'total_tokens':3}}
        raw=json.dumps(payload).encode();response=Mock();response.status_code=200
        response.headers={'Content-Type':'application/json'};response.raw=None
        response.iter_content.return_value=iter((raw,));response.__enter__=Mock(return_value=response)
        response.__exit__=Mock(return_value=False)
        client=Mock();client.post.return_value=response
        client.__enter__=Mock(return_value=client);client.__exit__=Mock(return_value=False)
        with patch('webapp.egress_policy.session',return_value=client):result=gateway.forward_result({'input':[],'stream':True})
        self.assertFalse(client.post.call_args.kwargs['json']['stream'])
        self.assertEqual(result.headers['Content-Type'],'text/event-stream')
        stream=StructuredStream('responses',lambda _:None)
        for chunk in result.chunks:stream.feed(chunk)
        self.assertEqual(stream.finish(),payload)

    def test_node_authorization_and_business_adapter_share_one_result_contract(self):
        from webapp.node_gateway import GrantAuthority,InMemoryGrantStore,NodeGatewayAuthorizer,NodeGatewayCore
        store=InMemoryGrantStore(lambda:100);issued=GrantAuthority(store).issue(
            task_id='task',attempt_id='attempt',generation=1,sandbox_id='sandbox',
            source_ip='172.30.0.8',routes={'/v1/responses'},expires_at=200,
            max_calls=1,max_request_bytes=100,max_response_bytes=100)
        gateway=Mock();gateway.model_adapter.protocol='responses';gateway.workflow_callback=None
        gateway.lease=None;gateway.check=Mock()
        closed=[];gateway.forward_result.return_value=GatewayResult(200,{},[b'ok'],lambda:closed.append(True))
        core=NodeGatewayCore(NodeGatewayAuthorizer(store),GatewayBusinessCore(gateway).handlers())
        headers={'Authorization':'Bearer '+issued.token,'X-Video-Use-Attempt':'attempt',
            'X-Video-Use-Generation':'1','X-Video-Use-Sandbox':'sandbox'}
        result=core.dispatch(headers,'/v1/responses','172.30.0.8',b'{"input":[]}')
        self.assertEqual(b''.join(result),b'ok');self.assertEqual(closed,[True])

    def test_context_and_workflow_routes_share_lease_bound_business_methods(self):
        gateway=Mock();gateway.model_adapter.protocol='responses';gateway.mode='plan';gateway.broker=None
        gateway.workflow_callback=Mock(return_value={'status':'ready'});gateway.check=Mock()
        lease=Mock();lease.kind='agent';lease.run_id='task';lease.bind.return_value=contextlib.nullcontext()
        lease.store.query.return_value=[{'project':'project'}];gateway.lease=lease
        core=GatewayBusinessCore(gateway)
        with patch('webapp.harness_context.lookup',return_value={'turns':[]}) as lookup:
            result=core.dispatch_value('/context',{'action':'turns'})
        self.assertEqual(json.loads(b''.join(result.chunks)),{'turns':[]})
        lookup.assert_called_once_with(lease.store,'project',{'action':'turns'})
        denied=core.dispatch_value('/workflow',{'action':'submit'})
        self.assertEqual(denied.status,403);gateway.workflow_callback.assert_not_called()
        invalid=core.dispatch_value('/workflow',{'action':'unknown'})
        self.assertEqual(invalid.status,422)
        accepted=core.dispatch_value('/workflow',{'action':'status'})
        self.assertEqual(json.loads(b''.join(accepted.chunks)),{'status':'ready'})
        self.assertGreaterEqual(gateway.check.call_count,4)

    def test_file_tool_routes_use_gateway_results_without_handler_objects(self):
        from webapp.agent_gateway import Gateway
        gateway=object.__new__(Gateway);gateway.mode='plan';gateway.assets={};gateway.broker=None
        self.assertEqual(gateway.image_result({'target_asset':'foreign','prompt':'x'}).status,403)
        self.assertEqual(gateway.transcribe_result({'asset':'foreign'}).status,404)
        gateway.canvas_callback=None
        self.assertEqual(gateway.canvas_result({'apply':True}).status,403)


if __name__=='__main__':unittest.main()
