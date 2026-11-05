import unittest

from fastapi.testclient import TestClient

from webapp.node_gateway import (GatewayResult,GrantAuthority,InMemoryGrantStore,
    NodeGatewayAuthorizer,NodeGatewayCore)
from webapp.node_gateway_service import create_app


class NodeGatewayServiceTests(unittest.TestCase):
    def setUp(self):
        self.now=[100.0];self.store=InMemoryGrantStore(lambda:self.now[0])
        authority=GrantAuthority(self.store)
        self.issued=authority.issue(task_id='task',attempt_id='attempt',generation=1,
            sandbox_id='sandbox',source_ip='172.30.0.8',routes={'/v1/responses'},
            expires_at=200,max_calls=2,max_request_bytes=100,max_response_bytes=100)
        self.closed=[]
        core=NodeGatewayCore(NodeGatewayAuthorizer(self.store),{'/v1/responses':lambda body,permit:
            GatewayResult(200,{'Content-Type':'text/event-stream','Set-Cookie':'secret'},
                [b'data: one\n\n',b'data: two\n\n'],lambda:self.closed.append(True))})
        app=create_app(core,self.store,source_ip=lambda request:'172.30.0.8',
            reaper_interval=.05,require_https=False)
        self.client=TestClient(app);self.client.__enter__();self.addCleanup(self.client.__exit__,None,None,None)
        self.headers={'Authorization':'Bearer '+self.issued.token,
            'X-Video-Use-Attempt':'attempt','X-Video-Use-Generation':'1',
            'X-Video-Use-Sandbox':'sandbox','X-Forwarded-For':'203.0.113.1'}

    def test_authorized_post_streams_and_filters_response_headers(self):
        response=self.client.post('/v1/responses',headers=self.headers,content=b'{}')
        self.assertEqual((response.status_code,response.content),
            (200,b'data: one\n\ndata: two\n\n'))
        self.assertEqual(response.headers['content-type'],'text/event-stream')
        self.assertNotIn('set-cookie',response.headers);self.assertEqual(self.closed,[True])

    def test_missing_token_query_and_budget_fail_without_handler_details(self):
        self.assertEqual(self.client.post('/v1/responses',content=b'{}').status_code,403)
        self.assertEqual(self.client.post('/v1/responses?target=other',headers=self.headers,content=b'{}').status_code,400)
        oversized=self.client.post('/v1/responses',headers=self.headers,content=b'x'*101)
        self.assertEqual(oversized.status_code,429)

    def test_reaper_closes_stalled_expired_connection(self):
        permit=NodeGatewayAuthorizer(self.store).authorize(
            self.headers,'/v1/responses','172.30.0.8',1)
        grant=self.store.by_id[self.issued.id]
        closer=[];permit.register_connection(lambda:closer.append(True))
        self.now[0]=201
        for _ in range(100):
            if closer:break
            __import__('time').sleep(.01)
        self.assertEqual(closer,[True]);self.assertTrue(grant.revoked)

    def test_default_application_rejects_plain_http_business_requests(self):
        core=NodeGatewayCore(NodeGatewayAuthorizer(self.store),{})
        with TestClient(create_app(core,self.store)) as client:
            self.assertEqual(client.post('/context',content=b'{}').status_code,426)


if __name__=='__main__':unittest.main()
