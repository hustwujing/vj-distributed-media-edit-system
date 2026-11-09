import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock,patch

from webapp.public_http_gateway import PublicHTTPGateway,ROUTE


class Response:
    def __init__(self,status=200,data=b'public data'):
        self.status_code=status;self.content=data
        self.headers={'Content-Type':'text/plain'};self.closed=False
    def close(self):self.closed=True


class Session:
    def __init__(self,response):self.response=response;self.calls=[]
    def __enter__(self):return self
    def __exit__(self,*_):pass
    def get(self,*args,**kwargs):self.calls.append((args,kwargs));return self.response


class PublicHTTPGatewayTests(unittest.TestCase):
    def setUp(self):
        self.lease=Mock();self.lease.run_id='run';self.url='https://example.com/data.json?version=1'

    def test_exact_grant_is_read_only_redirect_free_and_bounded(self):
        response=Response();session=Session(response)
        with patch('webapp.egress_policy.session',return_value=session),patch('webapp.egress_policy.denial'):
            result=PublicHTTPGateway(self.lease,[self.url]).handle(json.dumps({'url':self.url}).encode())
        self.assertEqual(result.status,200);self.assertEqual(b''.join(result.chunks),b'public data')
        self.assertEqual(session.calls[0][0],(self.url,))
        self.assertFalse(session.calls[0][1]['allow_redirects']);self.assertTrue(session.calls[0][1]['stream'] is False)
        self.lease.check.assert_called_once();result.close();self.assertTrue(response.closed)
        response=Response();response.headers['Content-Type']='text/plain\r\nX-Injected: yes'
        with patch('webapp.egress_policy.session',return_value=Session(response)):
            result=PublicHTTPGateway(self.lease,[self.url]).handle({'url':self.url})
        self.assertEqual(result.headers['Content-Type'],'application/octet-stream')

    def test_ungranted_internal_or_mutating_requests_never_reach_network(self):
        session=Session(Response())
        with patch('webapp.egress_policy.session',return_value=session),patch('webapp.egress_policy.denial'):
            for body in ({'url':'https://example.com/other'},{'url':self.url,'method':'POST'}):
                result=PublicHTTPGateway(self.lease,[self.url]).handle(body)
                self.assertEqual(result.status,403)
        self.assertEqual(session.calls,[])
        from webapp.agent_runtime_capabilities import validate_public_url
        for url in ('http://example.com/a','https://127.0.0.1/a','https://169.254.169.254/a','https://user:pass@example.com/a'):
            with self.assertRaises(ValueError):validate_public_url(url)

    def test_gateway_business_exposes_route_only_for_frozen_task_grant(self):
        from webapp.gateway_business import GatewayBusinessCore
        gateway=Mock();gateway.model_adapter.protocol='responses';gateway.workflow_callback=None
        gateway.canvas_callback=None;gateway.lease=self.lease
        with patch('webapp.public_http_gateway.urls_for_lease',return_value=(self.url,)):
            self.assertIn(ROUTE,GatewayBusinessCore(gateway).routes())
        with patch('webapp.public_http_gateway.urls_for_lease',return_value=()):
            self.assertNotIn(ROUTE,GatewayBusinessCore(gateway).routes())

    def test_public_helper_writes_only_under_declared_output(self):
        import runpy,sys
        from io import BytesIO
        class Upstream(BytesIO):
            def __enter__(self):return self
            def __exit__(self,*_):pass
        helper=Path(__file__).resolve().parents[1]/'runtime/public/network_fetch.py'
        with tempfile.TemporaryDirectory() as folder,patch.dict('os.environ',{'VIDEO_USE_STEP_OUTPUT':folder}), \
             patch('urllib.request.urlopen',return_value=Upstream(b'abc')), \
             patch.object(sys,'argv',['network_fetch.py',self.url,'nested/data.bin']):
            runpy.run_path(str(helper),run_name='__main__')
            self.assertEqual((Path(folder)/'nested/data.bin').read_bytes(),b'abc')
