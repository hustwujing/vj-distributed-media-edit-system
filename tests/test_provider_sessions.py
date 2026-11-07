import io
import json
import os
import threading
import unittest
from http.server import BaseHTTPRequestHandler,ThreadingHTTPServer
from unittest.mock import patch
from PIL import Image
from tests import test_workflows as fixtures
from webapp.workflow_runtime import Adapter
from webapp import agent_sandbox,workflows

class ProviderSessionTests(unittest.TestCase):
    def test_workflow_erase_sends_custom_instruction_and_selection_reference(self):
        from email.parser import BytesParser
        from email.policy import default
        f=fixtures.WorkflowTests();f.setUp();server=None;received=[]
        try:
            class Handler(BaseHTTPRequestHandler):
                def log_message(self,*args):pass
                def do_POST(self):
                    body=self.rfile.read(int(self.headers['Content-Length']))
                    message=BytesParser(policy=default).parsebytes(('Content-Type: '+self.headers['Content-Type']+'\r\n\r\n').encode()+body)
                    received.extend(message.iter_parts())
                    self.send_response(202);self.end_headers();self.wfile.write(b'{"id":"erasure-job"}')
            server=ThreadingHTTPServer(('127.0.0.1',0),Handler);threading.Thread(target=server.serve_forever,daemon=True).start()
            env=agent_sandbox.prepare(f.store,f.project,'test','execute',f.turn)
            Image.new('RGB',(256,256),'blue').save(env['sources']/'source.png')
            f.store.execute('UPDATE assets SET metadata=? WHERE id=?',(json.dumps({'kind':'image','width':256,'height':256}),'asset'))
            os.chown(env['sources']/'source.png',env['uid'],env['gid'])
            (env['sources']/'source.png').chmod(0o600)
            env['sources'].chmod(0o755)
            instruction='Remove only the LOGO text, leaving a plain blue background.'
            f.submit([{'id':'erase','name':'erase','kind':'operation','operation':'image.erase',
                'inputs':{'source':{'asset':'asset'}},'parameters':{'box':[2,2,4,4],'prompt':instruction,'quality':'high'},
                'goal':'Remove selected lettering','on_failure':'pause','acceptance':[{'kind':'image','width':8,'height':8}]}])
            sockets=f.store.root/'sockets';sockets.mkdir();adapter=Adapter(f.store,'test',sockets)
            with patch.dict('webapp.workflow_runtime.ENDPOINTS',{'edit':(f'http://127.0.0.1:{server.server_port}','target_image')}):
                w=f.get()
                try:identifier=adapter.submit(w,w['steps'][0])
                except Exception as exc:self.fail(json.dumps(getattr(exc.__cause__,'diagnostic',{'error':str(exc)}),ensure_ascii=False))
                self.assertEqual(identifier,'erasure-job')
            fields={p.get_param('name',header='content-disposition'):p.get_payload(decode=True).decode() for p in received if not p.get_filename()}
            self.assertIn(instruction,fields['prompt']);self.assertIn('图2',fields['prompt']);self.assertEqual(fields['quality'],'high')
            pictures=[p for p in received if p.get_filename()]
            self.assertEqual([p.get_param('name',header='content-disposition') for p in pictures],['target_image','reference_images'])
            source=Image.open(io.BytesIO(pictures[0].get_payload(decode=True))).convert('RGB')
            guide=Image.open(io.BytesIO(pictures[1].get_payload(decode=True))).convert('RGB')
            self.assertEqual(source.getpixel((0,0)),guide.getpixel((0,0)))
            self.assertNotEqual(source.getpixel((3,3)),guide.getpixel((3,3)))
        finally:
            if server:server.shutdown();server.server_close()
            f.tearDown()

    def test_new_session_after_restart_preserves_provider_ownership(self):
        f=fixtures.WorkflowTests();f.setUp();server=None
        try:
            image=io.BytesIO();Image.new('RGB',(8,8),'red').save(image,format='PNG');data=image.getvalue();calls=[]
            states=iter(('queued','running','unexpected','succeeded'))
            class Handler(BaseHTTPRequestHandler):
                def log_message(self,*args):pass
                def do_POST(self):
                    calls.append('submit');self.rfile.read(int(self.headers.get('Content-Length',0)))
                    self.send_response(202);self.send_header('Set-Cookie','owner=example-owned-session; Path=/');self.end_headers();self.wfile.write(b'{"id":"job1"}')
                def do_GET(self):
                    if self.headers.get('Cookie')!='owner=example-owned-session':self.send_response(403);self.end_headers();return
                    self.send_response(200);self.end_headers()
                    self.wfile.write(data if '/artifacts/' in self.path else json.dumps({
                        'status':next(states),'artifacts':[{'id':'image1','name':'image.png'}]}).encode())
            server=ThreadingHTTPServer(('127.0.0.1',0),Handler);threading.Thread(target=server.serve_forever,daemon=True).start()
            env=agent_sandbox.prepare(f.store,f.project,'test','execute',f.turn)
            Image.new('RGB',(256,256),'blue').save(env['sources']/'source.png')
            f.store.execute('UPDATE assets SET metadata=? WHERE id=?',(json.dumps({'kind':'image','width':256,'height':256}),'asset'))
            sockets=f.store.root/'sockets';sockets.mkdir()
            f.submit();adapter=Adapter(f.store,'test',sockets)
            with patch.dict('webapp.workflow_runtime.ENDPOINTS',{'edit':(f'http://127.0.0.1:{server.server_port}','target_image')}):
                w=f.get();s=w['steps'][0];identifier=adapter.submit(w,s)
                workflows.change(f.store,w['id'],s['id'],external_id=identifier,status='waiting')
                adapter=Adapter(f.store,'test',sockets);w=f.get();s=w['steps'][0]
                self.assertEqual(adapter.poll(w,s)['status'],'queued')
                self.assertEqual(f.store.query('SELECT state FROM workflow_provider_calls')[0]['state'],'waiting')
                self.assertEqual(adapter.poll(w,s)['status'],'running')
                with self.assertRaisesRegex(ValueError,'Unsupported provider receipt state'):adapter.poll(w,s)
                self.assertEqual(f.store.query('SELECT state FROM workflow_provider_calls')[0]['state'],'running')
                record=adapter.poll(w,s);files=adapter.collect(w,s,record,lambda:False)
                self.assertEqual(len(files),1);self.assertEqual(calls,['submit'])
                public=json.dumps(workflows.public(f.get()))
                self.assertNotIn('example-owned-session',public)
                self.assertTrue(f.store.query('SELECT cookies FROM workflow_provider_calls')[0]['cookies'])
        finally:
            if server:server.shutdown();server.server_close()
            f.tearDown()

    def test_undersized_editor_input_is_rejected_before_any_external_call(self):
        from webapp.workflow_runtime import StepFailure
        f=fixtures.WorkflowTests();f.setUp()
        try:
            env=agent_sandbox.prepare(f.store,f.project,'test','execute',f.turn)
            Image.new('RGB',(8,8),'blue').save(env['sources']/'source.png')
            f.submit();sockets=f.store.root/'sockets';sockets.mkdir();adapter=Adapter(f.store,'test',sockets);w=f.get()
            with patch.object(adapter,'session') as session,self.assertRaises(StepFailure) as error:
                adapter.submit(w,w['steps'][0])
            self.assertEqual(error.exception.diagnostic['code'],'pre_submit')
            self.assertIs(error.exception.diagnostic['request_sent'],False)
            from webapp.execution_contracts import classify_error
            self.assertEqual(classify_error(error.exception.diagnostic,'submit'),'pre_submit')
            self.assertIn('256',error.exception.diagnostic['detail'])
            session.assert_not_called()
            self.assertFalse(f.store.query('SELECT request_key FROM workflow_provider_calls'))
        finally:f.tearDown()
