"""A persisted provider ID is reused after takeover; unknown POSTs never repeat."""
import http.client
from http.server import BaseHTTPRequestHandler,ThreadingHTTPServer
import json
import os
from pathlib import Path
import socket
import tempfile
import threading
import unittest
from unittest.mock import patch

from webapp import agent_store
from webapp.image_document import ImageTask
from webapp.provider_relay import ImageRelay
from webapp.store import Store
from webapp.task_leases import Coordinator


class ProviderRelayTests(unittest.TestCase):
    def run_case(self,accepted,erase=False,invalid_first=False,quota_first=False,intent_failure=False):
        posts=[];payloads=[]
        class Handler(BaseHTTPRequestHandler):
            def log_message(self,*_):pass
            def do_POST(self):
                payloads.append(self.rfile.read(int(self.headers.get('Content-Length',0))))
                posts.append(self.path)
                body=json.dumps({'id':'original-job'}).encode()
                self.send_response(202 if accepted else 503)
                self.send_header('Content-Length',str(len(body)));self.end_headers();self.wfile.write(body)
        upstream=ThreadingHTTPServer(('127.0.0.1',0),Handler)
        thread=threading.Thread(target=upstream.serve_forever,daemon=True);thread.start()
        try:
            with tempfile.TemporaryDirectory() as tmp, patch.dict('webapp.provider_relay.ENDPOINTS',
                    {'edit':(f'http://127.0.0.1:{upstream.server_port}','target_image')}):
                store=Store(Path(tmp)/'db');agent_store.initialize(store)
                project=store.add_project('owner','project')['id']
                job=store.add_job(project,'image',{'kind':'image'})
                coordinator=Coordinator(store,'node-a','test')
                lease=coordinator.claim(job['id'],'manual')
                task=ImageTask(action='erase',asset='source',box=[1,1,2,2],prompt='Remove the SALE label completely.',quality='high') if erase else ImageTask(action='generate',asset='source',prompt='blue')
                def request(relay,path='/api/jobs',invalid=None):
                    connection=http.client.HTTPConnection('localhost',timeout=10)
                    connection.sock=socket.socket(socket.AF_UNIX)
                    connection.sock.connect(str(relay.path))
                    body=b'--sample\r\nContent-Disposition: form-data; name="target_image"; filename="image.png"\r\nContent-Type: image/png\r\n\r\nimage-bytes\r\n--sample--\r\n'
                    if erase:
                        body=body.replace(b'--sample--\r\n',b'--sample\r\nContent-Disposition: form-data; name="reference_images"; filename="guide.png"\r\nContent-Type: image/png\r\n\r\nguide-bytes\r\n--sample\r\nContent-Disposition: form-data; name="prompt"\r\n\r\nuntrusted-override\r\n--sample--\r\n')
                    content_type='multipart/form-data; boundary=sample'
                    if invalid=='not_multipart':content_type='application/json';body=b'{}'
                    if invalid=='missing_image':body=body.replace(b'name="target_image"',b'name="unknown"')
                    connection.request('POST',path,body,{'Content-Type':content_type})
                    response=connection.getresponse();status=response.status;data=response.read();connection.close()
                    return status,json.loads(data)
                relay=ImageRelay(Path(tmp)/'first.sock',task,lease,os.getuid(),os.getgid())
                try:
                    if invalid_first:
                        for invalid in ('not_multipart','missing_image'):
                            failure=request(relay,invalid=invalid)
                            self.assertEqual(failure[0],422)
                            self.assertFalse(failure[1]['request_sent'])
                            self.assertFalse(relay.submitted)
                        self.assertEqual(posts,[])
                    if quota_first:
                        with patch.dict(os.environ,{'VIDEO_USE_USER_IMAGE_PROVIDER_DAILY':'0'}):
                            failure=request(relay)
                            self.assertEqual(failure[0],403)
                            self.assertFalse(failure[1]['request_sent'])
                            self.assertFalse(relay.submitted)
                    if intent_failure:
                        with patch('webapp.provider_calls.intent',side_effect=ValueError('injected admission failure')):
                            self.assertEqual(request(relay)[0],422)
                            self.assertFalse(relay.submitted)
                    if invalid_first or quota_first or intent_failure:
                        self.assertEqual(store.query('SELECT * FROM execution_provider_calls'),[])
                        self.assertEqual(store.query('SELECT * FROM workflow_provider_calls'),[])
                        self.assertEqual(store.query("SELECT * FROM execution_budgets WHERE category='image_provider'"),[])
                        self.assertEqual(store.query('SELECT * FROM execution_external_cleanup'),[])
                    first=request(relay)
                    second=request(relay)
                    self.assertEqual(first[0],202 if accepted else 502)
                    self.assertEqual(second[0],202 if accepted else 409)
                    self.assertEqual(request(relay,'/api/jobs/other/cancel')[0],403)
                finally:relay.close()
                self.assertEqual(posts,['/api/jobs'])
                self.assertEqual(len(store.query('SELECT * FROM execution_provider_calls')),1)
                self.assertEqual(len(store.query('SELECT * FROM workflow_provider_calls')),1)
                self.assertEqual(store.query("SELECT used FROM execution_budgets WHERE category='image_provider'")[0]['used'],1)
                if erase:
                    self.assertIn(task.prompt.encode(),payloads[0]);self.assertIn('图2'.encode(),payloads[0])
                    self.assertIn(b'guide-bytes',payloads[0]);self.assertIn(b'\r\n\r\nhigh\r\n',payloads[0])
                    self.assertNotIn(b'untrusted-override',payloads[0])
                lease.release(interrupted=True)
                replacement=Coordinator(store,'node-b','test').claim(job['id'],'manual')
                if accepted:
                    resumed=ImageRelay(Path(tmp)/'second.sock',task,replacement,os.getuid(),os.getgid())
                    try:self.assertEqual(request(resumed),(202,{'id':'original-job'}))
                    finally:resumed.close()
                    self.assertEqual(posts,['/api/jobs'])
                else:
                    with self.assertRaisesRegex(RuntimeError,'cannot be safely resumed'):
                        ImageRelay(Path(tmp)/'second.sock',task,replacement,os.getuid(),os.getgid())
        finally:
            upstream.shutdown();upstream.server_close()

    def test_resume_reuses_original_upstream_job(self):self.run_case(True)
    def test_unknown_submission_never_posts_again(self):self.run_case(False)
    def test_erase_uses_approved_description_not_untrusted_form_fields(self):self.run_case(True,erase=True)
    def test_invalid_local_requests_do_not_lock_submission_or_create_ghost_intents(self):self.run_case(True,invalid_first=True)
    def test_quota_admission_failure_leaves_relay_reusable(self):self.run_case(True,quota_first=True)
    def test_provider_intent_failure_rolls_back_quota_and_execution_intent(self):self.run_case(True,intent_failure=True)


if __name__=='__main__':unittest.main()
