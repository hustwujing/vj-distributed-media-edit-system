import unittest
import http.client

from webapp.runtime_gateway import GatewayClient,GatewayClientError,GatewayClientServer
from webapp.runtime_protocol import RuntimeIdentity


class Session:
    def __init__(self):self.calls=[];self.trust_env=True;self.closed=False
    def request(self,*args,**kwargs):self.calls.append((args,kwargs));return object()
    def close(self):self.closed=True


class RuntimeGatewayTests(unittest.TestCase):
    def setUp(self):
        self.session=Session();self.identity=RuntimeIdentity('attempt-1',2,'sandbox-1')
        self.token='g'*48
        self.client=GatewayClient('https://172.30.0.1:18880',self.token,self.identity,
            {'/v1/responses','/context'},'/run/video-use/gateway-ca.pem',session=self.session)

    def test_fixed_route_injects_identity_and_strips_harness_credentials(self):
        self.client.forward('POST','/v1/responses',{
            'Content-Type':'application/json','Authorization':'untrusted','Host':'evil',
            'OpenAI-Beta':'responses=v1','X-Stainless-Lang':'python'},b'{}')
        args,kwargs=self.session.calls[0]
        self.assertEqual(args,('POST','https://172.30.0.1:18880/v1/responses'))
        self.assertEqual(kwargs['headers']['Authorization'],'Bearer '+self.token)
        self.assertNotIn('Host',kwargs['headers'])
        self.assertEqual(kwargs['headers']['X-Video-Use-Generation'],'2')
        self.assertFalse(kwargs['allow_redirects']);self.assertTrue(kwargs['stream'])
        self.assertEqual(kwargs['verify'],'/run/video-use/gateway-ca.pem')
        self.assertFalse(self.session.trust_env)

    def test_method_route_and_body_are_fail_closed(self):
        for method,path,body in [('GET','/context',b'{}'),('POST','/admin',b'{}'),
                                 ('POST','/context',b''),('POST','/context',b'x'*(32*1024*1024+1))]:
            with self.subTest(method=method,path=path),self.assertRaises(GatewayClientError):
                self.client.forward(method,path,{},body)
        self.assertEqual(self.session.calls,[])

    def test_image_generation_route_has_bounded_provider_timeout(self):
        client=GatewayClient('https://172.30.0.1:18880',self.token,self.identity,
            {'/v1/images/generations'},'/run/video-use/gateway-ca.pem',session=self.session)
        client.forward('POST','/v1/images/generations',{'Content-Type':'application/json'},b'{}')
        self.assertEqual(self.session.calls[0][1]['timeout'],(5,300))

    def test_endpoint_and_route_cannot_select_arbitrary_destination(self):
        for endpoint in ('http://172.30.0.1:18880','https://user:pass@host','https://host/base','https://host?x=1'):
            with self.subTest(endpoint=endpoint),self.assertRaises(GatewayClientError):
                GatewayClient(endpoint,'g'*48,self.identity,{'/context'},'ca',session=Session())
        with self.assertRaises(GatewayClientError):
            GatewayClient('https://host','g'*48,self.identity,{'https://evil/path'},'ca',session=Session())

    def test_loopback_server_streams_response_and_rejects_ungranted_routes(self):
        class Response:
            status_code=200;headers={'Content-Type':'text/event-stream'};closed=False
            def iter_content(self,chunk_size):return iter((b'data: one\n\n',b'data: two\n\n'))
            def close(self):self.closed=True
        response=Response();calls=[]
        class Client:
            def forward(self,*args):calls.append(args);return response
            def close(self):pass
        server=GatewayClientServer(Client(),port=0);server.start();self.addCleanup(server.close)
        connection=http.client.HTTPConnection(*server.address,timeout=2)
        connection.request('POST','/v1/responses',body=b'{}',headers={'Content-Type':'application/json'})
        result=connection.getresponse()
        self.assertEqual((result.status,result.read()),(200,b'data: one\n\ndata: two\n\n'))
        self.assertTrue(response.closed);self.assertEqual(calls[0][1],'/v1/responses')

        connection=http.client.HTTPConnection(*server.address,timeout=2)
        connection.request('POST','/v1/messages?beta=true',body=b'{}',headers={'Content-Type':'application/json'})
        accepted=connection.getresponse();self.assertEqual(accepted.status,200);accepted.read()
        self.assertEqual(calls[-1][1],'/v1/messages')

        for target in ('/context?target=other','/v1/messages?beta=false','/v1/responses?beta=true'):
            connection=http.client.HTTPConnection(*server.address,timeout=2)
            connection.request('POST',target,body=b'{}')
            denied=connection.getresponse();self.assertEqual(denied.status,403);denied.read()


if __name__=='__main__':unittest.main()
