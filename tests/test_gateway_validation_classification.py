"""Malformed requests and active egress violations have different outcomes."""
import json
import unittest
from unittest.mock import patch

from tests import test_execution_leases as fixtures
from webapp.agent_gateway import Gateway
from webapp.agent_profiles import AgentProfile
from webapp.model_adapters import ResponsesAdapter, ChatCompletionsAdapter, AnthropicAdapter


class GatewayValidationTests(unittest.TestCase):
    def setUp(self):
        fixture=fixtures.ExecutionLeaseTests()
        fixture.setUp()
        self.addCleanup(fixture.tearDown)
        self.store=fixture.store
        self.turn=fixture.turn
        self.lease=fixture.a.claim(self.turn,'agent')
        self.addCleanup(self.lease.release)
        self.gateway=object.__new__(Gateway)
        self.gateway.check=self.lease.check
        self.gateway.lease=self.lease
        self.gateway.model='model'
        self.gateway.base_url='https://provider.invalid'
        self.gateway.key='test-protected-credential-12345'
        self.gateway.profile=AgentProfile()
        self.gateway.model_adapter=ResponsesAdapter()

    def events(self):
        return self.store.query('SELECT * FROM execution_security_events WHERE task_id=?',(self.turn,))

    def test_eight_malformed_requests_do_not_terminate_task_or_spend_calls(self):
        payload={'input':[]}
        nested=payload
        for _ in range(64):
            nested['nested']={}
            nested=nested['nested']
        with patch('webapp.egress_policy.session') as network:
            for _ in range(8):
                result=self.gateway.forward_result(payload)
                self.assertEqual(result.status,422)
                self.assertEqual(json.loads(b''.join(result.chunks))['error']['code'],'model_request_invalid')
            network.assert_not_called()
        self.assertEqual(self.events(),[])
        self.assertEqual(self.store.query('SELECT termination_reason FROM execution_limits WHERE task_id=?',(self.turn,))[0]['termination_reason'],'')
        self.assertFalse(self.store.query('SELECT * FROM execution_budgets WHERE run_id=?',(self.turn,)))

    def test_real_credential_disclosure_retains_run_security_threshold(self):
        for _ in range(8):
            result=self.gateway.forward_result({'input':self.gateway.key})
            self.assertEqual(result.status,403)
            self.assertNotIn(self.gateway.key,b''.join(result.chunks).decode())
        self.assertEqual(len(self.events()),8)
        self.assertEqual(self.store.query('SELECT termination_reason FROM execution_limits WHERE task_id=?',(self.turn,))[0]['termination_reason'],'security')

    def test_remote_capabilities_still_count_as_policy_denials(self):
        requests=[(ResponsesAdapter(),{'input':[{'type':'input_image','image_url':'https://untrusted.invalid/a.png'}]}),
                  (ChatCompletionsAdapter(),{'messages':[],'tools':[{'type':'web_search'}]}),
                  (AnthropicAdapter(),{'messages':[],'tools':[{'type':'web_search_20250305','name':'web_search'}]})]
        for adapter,payload in requests:
            with self.subTest(protocol=adapter.protocol):
                self.gateway.model_adapter=adapter
                self.assertEqual(self.gateway.forward_result(payload).status,403)
        self.assertEqual(len(self.events()),3)

    def test_bad_server_destination_is_not_a_user_security_violation(self):
        self.gateway.base_url='file:///invalid'
        result=self.gateway.forward_result({'input':[]})
        self.assertEqual(result.status,503)
        self.assertEqual(self.events(),[])
