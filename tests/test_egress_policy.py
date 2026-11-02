import base64
import unittest
from unittest.mock import patch
from webapp.egress_policy import model_input,provider_base,session


class EgressTests(unittest.TestCase):
    def test_broker_permission_failure_keeps_type_and_hides_paths(self):
        import contextlib
        from unittest.mock import Mock
        from webapp.gateway_process import Client,GatewayProcess
        gateway=Mock();gateway.mode='execute';gateway.workflow_review_id=None
        gateway.lease.bind.side_effect=lambda:contextlib.nullcontext()
        gateway.workflow_callback.side_effect=PermissionError('PRIVATE /host/path')
        process=GatewayProcess(gateway);client=Client(process.client.detach());process.thread.start()
        try:
            with self.assertRaises(PermissionError) as error:client.request('workflow_preflight',{'action':'preflight'})
            self.assertNotIn('PRIVATE',str(error.exception))
            reply=Mock();client.relay('workflow_preflight',{'action':'preflight'},reply)
            status,body=reply.reply.call_args.args
            self.assertEqual(status,500)
            self.assertEqual(body['error']['code'],'filesystem_permission_denied')
            self.assertNotIn('PRIVATE',str(body))
        finally:
            client.socket.close();process.thread.join(timeout=2);process.server.close()
        self.assertFalse(process.thread.is_alive())

    def test_transcript_refresh_rejects_unscoped_files_before_changing_permissions(self):
        import contextlib,os,tempfile
        from pathlib import Path
        from unittest.mock import Mock
        from webapp.gateway_process import Client,GatewayProcess
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);folder=root/'edit/transcripts';folder.mkdir(parents=True)
            outside=root/'outside';outside.write_text('{}');outside.chmod(0o600)
            source=folder/'speech.json';source.symlink_to(outside)
            gateway=Mock();gateway.allowed_paths=None;gateway.project_dir=root
            gateway.assets={'speech':{'id':'speech','filename':'speech.wav','metadata':{'audio_tracks':[{}]}}}
            gateway.lease.bind.side_effect=lambda:contextlib.nullcontext()
            process=GatewayProcess(gateway);client=Client(process.client.detach());process.thread.start()
            try:
                for body in ({'asset':'foreign'},{'asset':'speech','track':2},{'asset':'speech'}):
                    with self.subTest(body=body),self.assertRaises(ValueError):client.request('refresh_transcript',body)
                source.unlink();os.link(outside,source)
                with self.assertRaises(ValueError):client.request('refresh_transcript',{'asset':'speech'})
                self.assertEqual(outside.stat().st_mode&0o777,0o600)
                source.unlink();source.write_text('{}');source.chmod(0o600)
                gateway.allowed_paths={'/image-tools'}
                with self.assertRaises(ValueError):client.request('refresh_transcript',{'asset':'speech'})
                self.assertEqual(source.stat().st_mode&0o777,0o600)
                gateway.lease.store.artifacts.save_file.assert_not_called()
            finally:
                client.socket.close();process.thread.join(timeout=2);process.server.close()

    def test_broker_distinguishes_validation_conditions_from_runtime_failure(self):
        import contextlib
        from unittest.mock import Mock
        from webapp.gateway_process import GatewayProcess,send,receive
        for kind in ('validation','planning_scope','runtime'):
            with self.subTest(kind=kind):
                gateway=Mock();gateway.mode='plan' if kind=='planning_scope' else 'execute';gateway.workflow_review_id=None
                gateway.lease.bind.side_effect=lambda:contextlib.nullcontext()
                gateway.workflow_callback.side_effect=ValueError('PRIVATE invalid graph') if kind=='validation' else RuntimeError('PRIVATE internal failure')
                process=GatewayProcess(gateway);process.thread.start()
                try:
                    send(process.client,{'operation':'workflow_preflight','body':{'action':'preflight'}})
                    response=receive(process.client)
                    self.assertEqual(response['status'],422)
                    self.assertNotIn('PRIVATE',str(response));self.assertNotIn('权限已失效',str(response))
                    if kind!='runtime':self.assertEqual(response['body']['error']['code'],'request_validation_failed')
                    if kind=='planning_scope':gateway.workflow_callback.assert_not_called()
                finally:
                    process.client.close();process.thread.join(timeout=2);process.server.close()
                self.assertFalse(process.thread.is_alive())

    def test_broker_preflight_reports_invalid_fields_without_private_values(self):
        import contextlib
        from unittest.mock import Mock
        from pydantic import BaseModel, ConfigDict, ValidationError
        from webapp.gateway_process import Client,GatewayProcess
        class Request(BaseModel):
            model_config=ConfigDict(extra='forbid')
            count:int
        try:Request.model_validate({'count':'PRIVATE input value','unknown':'PRIVATE extra value'})
        except ValidationError as exc:failure=exc
        gateway=Mock();gateway.mode='execute';gateway.workflow_review_id=None
        gateway.lease.bind.side_effect=lambda:contextlib.nullcontext()
        gateway.workflow_callback.side_effect=failure
        process=GatewayProcess(gateway);client=Client(process.client.detach());process.thread.start()
        try:
            with self.assertRaises(ValueError) as error:client.request('workflow_preflight',{'action':'preflight'})
            message=str(error.exception)
            self.assertIn('count: int_parsing',message);self.assertIn('unknown: extra_forbidden',message)
            self.assertNotIn('PRIVATE',message);self.assertNotIn('权限',message)
            gateway.workflow_callback.side_effect=None;gateway.workflow_callback.return_value={'passed':True}
            self.assertEqual(client.request('workflow_preflight',{'action':'preflight'}),{'passed':True})
        finally:
            client.socket.close();process.thread.join(timeout=2);process.server.close()
        self.assertFalse(process.thread.is_alive())

    def test_broker_preserves_typed_quota_across_process_protocol(self):
        import contextlib
        from unittest.mock import Mock
        from webapp.gateway_process import Client,GatewayProcess
        from webapp.task_leases import TaskQuotaReached
        gateway=Mock();gateway.mode='execute';gateway.workflow_review_id=None
        gateway.lease.bind.side_effect=lambda:contextlib.nullcontext()
        gateway.workflow_callback.side_effect=TaskQuotaReached('No remaining calls',category='image',scope='daily',used=2,limit=2)
        process=GatewayProcess(gateway);client=Client(process.client.detach());process.thread.start()
        try:
            with self.assertRaises(TaskQuotaReached) as error:client.request('workflow_preflight',{'action':'preflight'})
            self.assertEqual(error.exception.diagnostic,{'code':'daily_quota_exhausted','category':'image','scope':'daily','used':2,'limit':2})
            gateway.workflow_callback.assert_called_once()
        finally:
            client.socket.close();process.thread.join(timeout=2);process.server.close()
        self.assertFalse(process.thread.is_alive())

    def test_gateway_distinguishes_json_validation_and_quota_errors(self):
        import http.client,json,os,socket,tempfile
        from pathlib import Path
        from webapp.agent_gateway import Gateway
        from webapp.task_leases import TaskQuotaReached
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder);gateway=Gateway(root/'g.sock',root,[],'execute','model','https://provider.invalid','test-key',os.getuid(),os.getgid(),lambda *_:None,lambda *_:None)
            gateway.start()
            def post(body):
                conn=http.client.HTTPConnection('localhost');conn.sock=socket.socket(socket.AF_UNIX);conn.sock.connect(str(gateway.socket_path))
                conn.request('POST','/responses',body,{'Content-Type':'application/json'})
                reply=conn.getresponse();status=reply.status;value=json.loads(reply.read());conn.close();return status,value
            try:
                self.assertEqual(post(b'{')[1]['error']['code'],'invalid_json')
                self.assertEqual(post(b'[]')[1]['error']['code'],'invalid_request')
                with patch.object(gateway,'forward',side_effect=ValueError('PRIVATE implementation detail')):
                    status,value=post(b'{}');self.assertEqual(status,422);self.assertEqual(value['error']['code'],'request_validation_failed');self.assertNotIn('PRIVATE',json.dumps(value))
                with patch.object(gateway,'forward',side_effect=TaskQuotaReached('额度已用完',category='model',scope='task',used=40,limit=40)):
                    status,value=post(b'{}');self.assertEqual(status,403);self.assertEqual(value['error']['code'],'task_quota_exhausted');self.assertEqual(value['error']['used'],40)
            finally:gateway.close()

    def test_gateway_turn_limit_does_not_spend_another_task_call(self):
        import threading
        from unittest.mock import Mock
        from webapp.agent_gateway import Gateway
        from webapp.task_leases import TaskQuotaReached
        gateway=object.__new__(Gateway);gateway.check=Mock();gateway.broker=None
        from webapp.agent_profiles import AgentProfile
        from webapp.model_adapters import ResponsesAdapter
        gateway.profile=AgentProfile();gateway.model_adapter=ResponsesAdapter()
        gateway.lease=Mock();gateway.lease.runtime=None;gateway.lock=threading.Lock();gateway.calls=gateway.profile.budget['run_models']
        gateway.model='model';gateway.base_url='https://provider.invalid';gateway.key='test-key'
        with patch('webapp.agent_gateway.quota_status',return_value={'blocked':False}),self.assertRaises(TaskQuotaReached) as error:
            gateway.forward(Mock(),{'input':[]})
        self.assertEqual(error.exception.diagnostic['scope'],'turn');gateway.lease.consume.assert_not_called()

    def test_model_health_uses_http_status_not_error_words(self):
        import threading
        from unittest.mock import Mock
        from webapp.agent_gateway import Gateway
        from webapp.task_leases import LeaseLost
        gateway=object.__new__(Gateway);gateway.check=Mock();gateway.broker=None;gateway.lease=None
        from webapp.agent_profiles import AgentProfile
        from webapp.model_adapters import ResponsesAdapter
        gateway.profile=AgentProfile();gateway.model_adapter=ResponsesAdapter()
        gateway.lock=threading.Lock();gateway.calls=0;gateway.model='model';gateway.base_url='https://provider.invalid';gateway.key='test-key';gateway.note_model_unavailable=Mock()
        client=Mock();client.__enter__=Mock(return_value=client);client.__exit__=Mock(return_value=False)
        response=Mock();response.__enter__=Mock(return_value=response);response.__exit__=Mock(return_value=False);client.post.return_value=response
        with patch('webapp.egress_policy.session',return_value=client):
            response.status_code=400
            gateway.forward(Mock(),{'input':[{'type':'input_text','text':'503 connection are ordinary words here'}]})
            gateway.note_model_unavailable.assert_not_called()
            response.status_code=503;gateway.forward(Mock(),{'input':[]})
            gateway.note_model_unavailable.assert_called_once()
        gateway.lease=Mock();gateway.lease.check.side_effect=LeaseLost('cancelled')
        Gateway.note_model_unavailable(gateway);gateway.lease.store.execute.assert_not_called()

    def test_known_credentials_split_across_export_chunks_are_blocked(self):
        import io
        from webapp.egress_policy import copy_public
        secret='test-only-private-credential-value'
        source=io.BytesIO(b'x'*(1024*1024-5)+secret.encode())
        with patch.dict('os.environ',{'TEST_PRIVATE_KEY':secret}),self.assertRaisesRegex(ValueError,'credential'):
            copy_public(source,io.BytesIO())

    def test_stuck_dns_resolution_does_not_block_a_request_indefinitely(self):
        import threading,time
        from webapp.egress_policy import resolve
        release=threading.Event()
        def stuck(*args,**kw):release.wait(2);return []
        try:
            with patch('socket.getaddrinfo',side_effect=stuck):
                started=time.monotonic()
                with self.assertRaisesRegex(OSError,'timed out'):resolve('provider.example',443,.02)
                self.assertLess(time.monotonic()-started,1)
        finally:release.set()

    def test_text_urls_are_data_but_active_remote_images_are_denied(self):
        model_input({'input':[{'type':'input_text','text':'说明 http://example.com 的含义'}]})
        for url in ('http://127.0.0.1','http://169.254.169.254','file:///etc/passwd','https://example.com/image.png'):
            with self.subTest(url=url),self.assertRaises(ValueError):model_input({'input':[{'type':'input_image','image_url':url}]})

    def test_inline_svg_and_external_schema_references_are_denied(self):
        disguised='data:image/png;base64,'+base64.b64encode(b'<svg><image href="http://localhost"/></svg>').decode()
        with self.assertRaises(ValueError):model_input({'input':[{'type':'input_image','image_url':disguised}]})
        with self.assertRaises(ValueError):model_input({'tools':[{'type':'function','parameters':{'$ref':'http://localhost/schema'}}]})

    def test_remote_tools_and_cross_conversation_references_are_removed(self):
        result=model_input({'input':[],'tools':[{'type':'web_search'},{'type':'mcp','server_url':'http://localhost'},{'type':'function','name':'local'}],
                            'conversation':'other-user','previous_response_id':'other-id','proxy':'http://localhost'})
        self.assertEqual(result['tools'],[{'type':'function','name':'local'}])
        self.assertNotIn('conversation',result);self.assertNotIn('proxy',result)

    def test_metadata_destinations_and_credentials_in_provider_url_are_denied(self):
        for value in ('http://100.100.100.200','http://[fd00:ec2::254]','http://user:secret@example.com','file:///etc/passwd'):
            with self.subTest(url=value),self.assertRaises(ValueError):provider_base(value)

    def test_dns_to_internal_address_is_rejected_before_connect(self):
        import socket
        import requests
        answer=[(socket.AF_INET,socket.SOCK_STREAM,6,'',('169.254.169.254',80))]
        with patch('socket.getaddrinfo',return_value=answer),patch('socket.socket.connect') as connect:
            with session() as client,self.assertRaises(requests.RequestException):client.get('http://public-provider.example',timeout=1)
            connect.assert_not_called()

if __name__=='__main__':unittest.main()

class CancellationTransportTests(unittest.TestCase):
    def test_cancel_interrupts_inflight_provider_wait_without_resubmission(self):
        import threading,time,tempfile
        from pathlib import Path
        from http.server import BaseHTTPRequestHandler,ThreadingHTTPServer
        from webapp import agent_store
        from webapp.store import Store
        from webapp.task_leases import Coordinator
        entered=threading.Event();release=threading.Event();finished=threading.Event();calls=[];errors=[]
        class Handler(BaseHTTPRequestHandler):
            def log_message(self,*args):pass
            def do_GET(self):
                calls.append(self.path);entered.set();release.wait(10)
                try:self.send_response(200);self.end_headers();self.wfile.write(b'{}')
                except OSError:pass
        server=ThreadingHTTPServer(('127.0.0.1',0),Handler);threading.Thread(target=server.serve_forever,daemon=True).start()
        try:
            with tempfile.TemporaryDirectory() as directory:
                store=Store(Path(directory));agent_store.initialize(store);project=store.add_project('owner','cancel')['id']
                turn=agent_store.add_turn(store,project,'execute','test');lease=Coordinator(store,'node','test').claim(turn,'agent')
                def request():
                    try:
                        with lease.bind(),session() as client:client.get(f'http://127.0.0.1:{server.server_port}/wait',timeout=8)
                    except Exception as exc:errors.append(type(exc).__name__)
                    finally:finished.set()
                thread=threading.Thread(target=request);thread.start()
                self.assertTrue(entered.wait(3))
                store.execute("UPDATE agent_turns SET status='cancelled' WHERE id=?",(turn,))
                self.assertTrue(finished.wait(4),'cancelled request remained blocked')
                thread.join();self.assertTrue(errors);self.assertEqual(calls,['/wait'])
                lease.release()
        finally:release.set();server.shutdown();server.server_close()
