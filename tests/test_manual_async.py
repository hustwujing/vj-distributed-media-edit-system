import io
import json
import os
from pathlib import Path
import shutil
import tempfile
import threading
import unittest
from unittest.mock import patch
from http.server import BaseHTTPRequestHandler,ThreadingHTTPServer
from PIL import Image
from webapp.agent_host import AgentHost
from webapp import agent_store
from webapp.store import Store
from webapp.execution_resources import IdentityPool

@unittest.skipUnless(os.geteuid()==0 and shutil.which('bwrap'),'Real runtime required')
class ManualAsyncTests(unittest.TestCase):
    def test_external_wait_releases_runtime_and_resumes_owned_job(self):
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder);store=Store(root/'data');agent_store.initialize(store)
            project=store.add_project('owner','manual')['id']
            buf=io.BytesIO();Image.new('RGB',(256,256),'blue').save(buf,'PNG');image=buf.getvalue()
            source=store.project_dir(project)/'sources/source.png';source.write_bytes(image)
            store.add_asset(project,'source','source.png','source.png',{'kind':'image','width':256,'height':256})
            job=store.add_job(project,'image',{'kind':'image','image_task':{'action':'generate','asset':'source','prompt':'make red'}})
            calls=[];complete=[False]
            class Handler(BaseHTTPRequestHandler):
                def log_message(self,*args):pass
                def do_POST(self):
                    self.rfile.read(int(self.headers.get('Content-Length',0)));calls.append(self.path)
                    self.send_response(202);self.send_header('Set-Cookie','owner=manual-session; Path=/');self.end_headers();self.wfile.write(b'{"id":"manual1"}')
                def do_GET(self):
                    if self.headers.get('Cookie')!='owner=manual-session':self.send_response(403);self.end_headers();return
                    self.send_response(200);self.end_headers()
                    self.wfile.write(image if '/artifacts/' in self.path else json.dumps({'status':'succeeded' if complete[0] else 'running','artifacts':[{'id':'out','name':'image.png'}]}).encode())
            server=ThreadingHTTPServer(('127.0.0.1',0),Handler);threading.Thread(target=server.serve_forever,daemon=True).start()
            try:
                with patch.dict(os.environ,{'VIDEO_USE_TASK_MEMORY_BYTES':str(256*1024**2),'VIDEO_USE_TASK_DISK_BYTES':str(64*1024**2)}),patch.dict('webapp.workflow_runtime.ENDPOINTS',{'edit':(f'http://127.0.0.1:{server.server_port}','target_image')}):
                    host=AgentHost(store.root);host.identities=IdentityPool(root/'node',start=470000,size=4)
                    lease=host.coordinator.claim(job['id'],'manual');host.execute_claim(lease)
                    self.assertEqual(store.query('SELECT state FROM manual_operation_runs')[0]['state'],'waiting')
                    self.assertIsNone(lease.runtime)
                    self.assertFalse(store.query('SELECT state FROM execution_cleanup'))
                    store.execute('UPDATE manual_operation_runs SET next_poll=0')
                    lease=host.coordinator.claim(job['id'],'manual');host.execute_claim(lease)
                    self.assertIsNone(lease.runtime)
                    complete[0]=True;store.execute('UPDATE manual_operation_runs SET next_poll=0')
                    lease=host.coordinator.claim(job['id'],'manual');host.execute_claim(lease)
                    self.assertEqual(store.job(job['id'])['status'],'succeeded',store.job(job['id']))
                    self.assertTrue(store.query('SELECT state FROM execution_cleanup'))
                    self.assertTrue(all(r['state']=='complete' for r in store.query('SELECT state FROM execution_cleanup')))
                    self.assertEqual(calls,['/api/jobs'])
                    self.assertFalse(store.query('SELECT * FROM agent_turns'))
            finally:server.shutdown();server.server_close()

    def test_small_manual_input_fails_without_provider_submission_or_unknown_state(self):
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder);store=Store(root/'data');agent_store.initialize(store)
            project=store.add_project('owner','small input')['id']
            Image.new('RGB',(8,8),'blue').save(store.project_dir(project)/'sources/source.png')
            store.add_asset(project,'source','source.png','source.png',{'kind':'image','width':8,'height':8})
            job=store.add_job(project,'image',{'kind':'image','image_task':{'action':'generate','asset':'source','prompt':'make red'}})
            host=AgentHost(store.root);host.identities=IdentityPool(root/'node',start=470000,size=4)
            with patch.object(host.workflow_runner.adapter,'session') as session:
                lease=host.coordinator.claim(job['id'],'manual');host.execute_claim(lease)
            session.assert_not_called();self.assertFalse(store.query('SELECT request_key FROM workflow_provider_calls'))
            self.assertEqual(store.job(job['id'])['status'],'failed')
            self.assertIn('未发起生成',store.job(job['id'])['error'])
            self.assertEqual(store.query('SELECT state FROM manual_operation_runs')[0]['state'],'failed')
