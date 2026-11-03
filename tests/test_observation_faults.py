"""Real isolated failures; no production services or paid model requests."""
from http.server import BaseHTTPRequestHandler,ThreadingHTTPServer
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import uuid
from unittest.mock import patch
import requests
from webapp import telemetry,agent_store
from webapp.diagnostics import diagnose
from webapp.observation_collector import Archive,storage_health
from webapp.store import Store


class ObservationFaultTests(unittest.TestCase):
    def test_real_script_failure_and_cgroup_oom_have_distinct_evidence(self):
        from webapp.execution_resources import Cgroups
        groups=Cgroups('obs-test-'+uuid.uuid4().hex,64*1024**2,1,16)
        process=None
        try:
            failed=subprocess.run([sys.executable,'-c','raise RuntimeError("PRIVATE SCRIPT CONTENT")'],capture_output=True)
            with patch('webapp.telemetry.emit') as emit:
                telemetry.process_exit(failed,groups.paths)
                self.assertEqual(emit.call_args.kwargs['returncode'],1);self.assertEqual(emit.call_args.kwargs['oom_kill'],0)
            process=subprocess.Popen([sys.executable,'-c','import sys;sys.stdin.readline();x=[bytearray(16*1024**2) for _ in range(50)]'],stdin=subprocess.PIPE,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
            for path in groups.paths:(path/'cgroup.procs').write_text(str(process.pid))
            process.stdin.write(b'run\n');process.stdin.close();process.wait(timeout=15)
            with patch('webapp.telemetry.emit') as emit:
                telemetry.process_exit(process,groups.paths)
                self.assertEqual(process.returncode,-9);self.assertGreater(emit.call_args.kwargs['oom_kill'],0)
        finally:
            if process and process.poll() is None:process.kill();process.wait()
            Cgroups.clear(groups.paths)
            for path in groups.paths:path.rmdir()

    def test_real_http_timeout_rejection_and_unknown_submission_are_diagnosable(self):
        calls=[]
        class Handler(BaseHTTPRequestHandler):
            def log_message(self,*args):pass
            def do_POST(self):
                calls.append(self.path)
                if self.path=='/unknown':self.close_connection=True;return
                time.sleep(.15)
                try:
                    self.send_response(503);self.send_header('X-Request-ID','provider-request-123');self.end_headers();self.wfile.write(b'{"error":{"code":"busy"}}')
                except OSError:pass
        server=ThreadingHTTPServer(('127.0.0.1',0),Handler);thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
        events=[]
        def collect(event,level='INFO',exc=None,**fields):events.append(telemetry.record(event,level,exc,**fields))
        client=requests.Session();client.trust_env=False;base='http://127.0.0.1:'+str(server.server_port)
        try:
            with telemetry.bind(task_id='a'*32,attempt_id='b'*32),patch('webapp.telemetry.emit',side_effect=collect):
                with self.assertRaises(requests.Timeout),telemetry.span('remote_sync'):client.post(base+'/slow',timeout=.03)
                with self.assertRaises(requests.ConnectionError),telemetry.span('submit'):client.post(base+'/unknown',timeout=1)
                from webapp.generation_errors import raise_for_status
                with self.assertRaises(requests.HTTPError),telemetry.span('poll'):raise_for_status(client.post(base+'/rejected',timeout=1))
            errors=[e for e in events if e['event']=='phase.error']
            self.assertEqual({e['phase'] for e in errors},{'remote_sync','submit','poll'})
            self.assertTrue(all(e['task_id']=='a'*32 and e['stack'] for e in errors))
            self.assertTrue(any(e.get('provider_request_id')=='provider-request-123' for e in events))
            self.assertEqual(calls.count('/unknown'),1)
        finally:server.shutdown();server.server_close();thread.join();client.close()

    def test_actual_database_and_object_storage_outages_preserve_partial_diagnostics(self):
        from tests.distributed_support import PostgresServer,S3Server
        from webapp.artifact_store import S3Objects
        pg=PostgresServer();s3=S3Server()
        try:
            with tempfile.TemporaryDirectory() as folder,patch.dict(os.environ,{'VIDEO_USE_OBJECT_BUCKET':'observe','VIDEO_USE_OBJECT_ENDPOINT':s3.endpoint,'AWS_ACCESS_KEY_ID':'test','AWS_SECRET_ACCESS_KEY':'test','AWS_DEFAULT_REGION':'us-east-1'}):
                store=Store(Path(folder)/'data',dsn=pg.dsn,timeout=.2);agent_store.initialize(store)
                project=store.add_project('owner','test')['id'];task=agent_store.add_turn(store,project,'plan','private')
                backend=S3Objects('observe',s3.endpoint);backend.client.create_bucket(Bucket='observe');store.artifacts
                archive=Archive(Path(folder)/'obs')
                self.assertEqual(storage_health(store,archive),'available')
                s3.process.terminate();s3.process.wait(timeout=5);archive.storage_probe_at=0
                self.assertEqual(storage_health(store,archive),'unavailable')
                pg.command('pg_ctl','-D',str(pg.root/'data'),'-m','immediate','-w','stop')
                with patch('webapp.diagnostics.observation',return_value={'events':[{'task_id':task,'event':'storage.failed'}]}):report=diagnose(store,task)
                self.assertFalse(report['complete']);self.assertTrue(report['logs']);self.assertTrue(report['gaps'])
        finally:s3.close();pg.close()

    def test_cancelled_unknown_provider_and_quarantined_cleanup_remain_distinct(self):
        from webapp.task_lifecycle import cancel_task
        with tempfile.TemporaryDirectory() as folder:
            store=Store(Path(folder));agent_store.initialize(store)
            p=store.add_project('owner','p')['id'];task=agent_store.add_turn(store,p,'plan','private')
            store.execute("INSERT INTO execution_external_cleanup(id,task_id,provider,external_id,state) VALUES (?,?,?,?,'unconfirmed')",('cleanup',task,'wan','upstream-id'))
            cancel_task(store,task)
            store.execute("INSERT INTO execution_cleanup(attempt,task_id,node,state,updated) VALUES (?,?,?,'quarantined',?)",('attempt',task,'node',time.time()))
            report=diagnose(store,task,include_logs=False)
            self.assertEqual(report['task']['status'],'cancelled')
            self.assertNotEqual(report['external_cleanup'][0]['state'],'cancelled')
            self.assertEqual(report['local_cleanup'][0]['state'],'quarantined')
            self.assertIn('check_cleanup',[s['code'] for s in report['suggestions']])

    def test_blocked_log_writer_has_bounded_queue_and_fast_business_return(self):
        # A separate process prevents the intentionally blocked writer from
        # contaminating any other suite test's stderr or queue.
        code='''import os,threading,time,json
from webapp import telemetry
class Blocked:
 def write(self,data):threading.Event().wait(30)
 def flush(self):pass
telemetry.sys.stdout=Blocked();os.environ['VIDEO_USE_TELEMETRY_ENABLED']='1'
start=time.monotonic()
for i in range(5000):telemetry.emit('test',task_id='a'*32)
result={'seconds':time.monotonic()-start,'queued':telemetry._QUEUE.qsize(),'dropped':telemetry._DROPPED}
os.write(2,json.dumps(result).encode())
'''
        result=subprocess.run([sys.executable,'-c',code],capture_output=True,timeout=10)
        data=json.loads(result.stderr);self.assertLessEqual(data['queued'],1024);self.assertGreater(data['dropped'],0);self.assertLess(data['seconds'],3)

    def test_short_lived_cleanup_child_keeps_safe_correlated_terminal_logs(self):
        with patch.dict(os.environ,{'VIDEO_USE_TELEMETRY_ENABLED':'1','VIDEO_USE_MODEL_API_KEY':'private-not-forwarded'}):
            env=telemetry.child_env('external_cleanup','a'*32,'b'*32)
        self.assertNotIn('VIDEO_USE_MODEL_API_KEY',env)
        code="from webapp import telemetry;telemetry.setup('child');telemetry.emit('cleanup.test');raise ValueError('DO NOT LOG USER CONTENT')"
        result=subprocess.run([sys.executable,'-c',code],env={**os.environ,**env},capture_output=True,timeout=5)
        records=[json.loads(line) for line in result.stderr.decode().splitlines()]
        self.assertTrue(any(r['event']=='cleanup.test' for r in records))
        self.assertTrue(any(r['event']=='process.uncaught' for r in records))
        self.assertTrue(all(r.get('task_id')=='a'*32 for r in records))
        self.assertNotIn('DO NOT LOG USER CONTENT',result.stderr.decode())

    def test_real_unprivileged_distributed_cleanup_child_logs_task_and_finishes(self):
        from tests.distributed_support import PostgresServer
        from webapp.execution_reaper import Reaper
        from webapp.execution_resources import IdentityPool
        pg=PostgresServer()
        try:
            with tempfile.TemporaryDirectory(prefix='vu-observe-cleanup-') as folder,patch.dict(os.environ,{
                'VIDEO_USE_DATABASE_URL':pg.dsn,'VIDEO_USE_DISTRIBUTED':'1','VIDEO_USE_OBJECT_BUCKET':'declaration-only',
                'VIDEO_USE_TELEMETRY_ENABLED':'1',
            }):
                root=Path(folder);root.chmod(0o755);store=Store(root/'data');agent_store.initialize(store)
                project=store.add_project('owner','cleanup')['id'];task=agent_store.add_turn(store,project,'plan','test')
                store.execute("INSERT INTO execution_external_cleanup(id,task_id,provider,external_id,state) VALUES ('one',?,'http://127.0.0.1:9898','job123','pending')",(task,))
                store.execute("INSERT INTO workflow_provider_calls(request_key,workflow,step,revision,attempt,provider,operation,state,external_id,inputs,created,updated,implementation) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    ('one',task,'step',1,1,'http://127.0.0.1:9898','video.generate_from_image','waiting','job123','{}',0,0,json.dumps({'protocol':'wan-video-v1','provider':'http://127.0.0.1:9898','route_adapter':'generation','cleanup_category':'generation_protocol'})))
                reaper=Reaper(store,IdentityPool(root/'registry',start=815000,size=2,reconcile=False))
                original=subprocess.Popen;children=[]
                def launch(args,**kwargs):
                    if 'webapp.external_cleanup' in args:
                        kwargs['stderr']=subprocess.PIPE;child=original(args,**kwargs);children.append(child);return child
                    return original(args,**kwargs)
                with patch('webapp.execution_reaper.subprocess.Popen',side_effect=launch):reaper.tick()
                self.assertEqual(len(children),1)
                _,stderr=children[0].communicate(timeout=15)
                self.assertEqual(children[0].returncode,0,stderr.decode())
                self.assertEqual(store.query("SELECT state FROM execution_external_cleanup WHERE id='one'")[0]['state'],'unsupported')
                events=[json.loads(line) for line in stderr.decode().splitlines()]
                self.assertTrue(any(e.get('task_id')==task and e.get('phase')=='external_cleanup' for e in events))
        finally:pg.close()

    def test_actual_manual_image_user_route_has_output_and_correlated_events(self):
        from tests.test_webapp import executor_app
        from PIL import Image
        import io
        events=[]
        def collect(event,level='INFO',exc=None,**fields):events.append(telemetry.record(event,level,exc,**fields))
        with tempfile.TemporaryDirectory(prefix='vu-observe-image-') as folder,patch('webapp.telemetry.emit',side_effect=collect):
            with executor_app(Path(folder)) as client:
                project=client.post('/api/projects',json={'name':'manual image observation'}).json()['id']
                content=io.BytesIO();Image.new('RGB',(128,96),'blue').save(content,'PNG')
                response=client.post(f'/api/projects/{project}/assets',files={'file':('input.png',content.getvalue(),'image/png')});self.assertEqual(response.status_code,201,response.text)
                asset=response.json()['id']
                response=client.post(f'/api/projects/{project}/image-jobs',json={'action':'local_adjust','asset':asset,'parameters':{'brightness':.7}})
                self.assertEqual(response.status_code,202,response.text);task=response.json()['id']
                end=time.monotonic()+30
                while time.monotonic()<end:
                    job=next(j for j in client.get(f'/api/projects/{project}').json()['jobs'] if j['id']==task)
                    if job['status'] in ('succeeded','failed','cancelled'):break
                    time.sleep(.2)
                errors=[e for e in events if e.get('task_id')==task and e.get('level')=='ERROR']
                self.assertEqual(job['status'],'succeeded',json.dumps(errors))
                artifact=next(a for a in job['artifacts'] if a['name'].endswith('.png'))
                image=Image.open(io.BytesIO(client.get('/'+artifact['url']).content))
                self.assertEqual(image.size,(128,96));self.assertLess(image.convert('RGB').getpixel((0,0))[2],255)
                self.assertTrue(any(e.get('task_id')==task and e['event']=='process.exited' for e in events))

    def test_actual_manual_image_route_uses_shared_database_in_relay_child(self):
        from tests.distributed_support import PostgresServer,S3Server
        from webapp.artifact_store import S3Objects
        pg=PostgresServer();s3=S3Server()
        try:
            with patch.dict(os.environ,{'VIDEO_USE_DATABASE_URL':pg.dsn,'VIDEO_USE_DISTRIBUTED':'1',
                'VIDEO_USE_OBJECT_BUCKET':'manual-image-observation','VIDEO_USE_OBJECT_ENDPOINT':s3.endpoint,'VIDEO_USE_SESSION_SECRET':'a1'*32,
                'AWS_ACCESS_KEY_ID':'test','AWS_SECRET_ACCESS_KEY':'test','AWS_DEFAULT_REGION':'us-east-1'}):
                backend=S3Objects('manual-image-observation',s3.endpoint);backend.client.create_bucket(Bucket='manual-image-observation')
                self.test_actual_manual_image_user_route_has_output_and_correlated_events()
        finally:s3.close();pg.close()
