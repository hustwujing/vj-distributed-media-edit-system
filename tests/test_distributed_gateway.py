"""Real unprivileged broker + PostgreSQL + encrypted S3-compatible files."""
import base64
import http.client
import io
import json
import os
from pathlib import Path
import shutil
import socket
import subprocess
import tempfile
import unittest
from unittest.mock import patch
from PIL import Image
from tests.distributed_support import PostgresServer,S3Server


@unittest.skipUnless(os.geteuid()==0 and shutil.which('bwrap'),'Privileged sandbox required')
class DistributedGatewayTests(unittest.TestCase):
    def test_root_owned_transcript_cache_survives_gateway_and_second_node_restore(self):
        pg=PostgresServer();s3=S3Server()
        try:
            with tempfile.TemporaryDirectory(prefix='vtg-') as directory,patch.dict(os.environ,{
                'VIDEO_USE_DATABASE_URL':pg.dsn,'VIDEO_USE_DISTRIBUTED':'1','VIDEO_USE_OBJECT_BUCKET':'transcript-files',
                'VIDEO_USE_OBJECT_ENDPOINT':s3.endpoint,'AWS_ACCESS_KEY_ID':'test','AWS_SECRET_ACCESS_KEY':'test',
                'AWS_DEFAULT_REGION':'us-east-1','VIDEO_USE_OBJECT_ENCRYPTION_KEY':base64.b64encode(b'x'*32).decode(),
                'VIDEO_USE_OBJECT_REQUIRE_ENCRYPTION':'1','VIDEO_USE_SESSION_SECRET':'ab'*32,
                'ELEVENLABS_API_KEY':'test-only-no-network',
                'VIDEO_USE_TASK_MEMORY_BYTES':str(512*1024**2),'VIDEO_USE_TASK_DISK_BYTES':str(64*1024**2)}):
                from webapp.app import create_app
                from webapp.store import Store
                from webapp.artifact_store import S3Objects
                from webapp.execution_resources import IdentityPool
                from webapp.execution_runtime import TaskRuntime
                from webapp.task_leases import Coordinator
                from webapp.agent_gateway import Gateway
                root=Path(directory);backend=S3Objects('transcript-files',s3.endpoint)
                backend.client.create_bucket(Bucket='transcript-files')
                origin=create_app(root/'origin',run_worker=False).state.store
                project=origin.add_project('transcript-owner','cached transcript')['id']
                source=origin.project_dir(project)/'sources/speech.wav';source.write_bytes(b'cached audio, never decoded')
                origin.add_asset(project,'speech','speech.wav','speech.wav',{'kind':'audio','duration':1,'audio_tracks':[{},{}]})
                relative='edit/transcripts/speech.track1.json'
                transcript={'text':'Already transcribed','words':[]}
                original=origin.project_dir(project)/relative;original.write_text(json.dumps(transcript))
                origin.artifacts.save_file(project,relative)
                for node in ('a','b'):
                    with self.subTest(node=node):
                        store=Store(root/node)
                        job=store.add_job(project,'transcribe',{'kind':'transcribe','assets':['speech'],'audio_track':1,'workers':1})
                        lease=Coordinator(store,'transcript-'+node,'test').claim(job['id'],'manual')
                        self.assertIsNotNone(lease)
                        runtime=None;gateway=None
                        try:
                            with lease.bind():
                                store.execute("UPDATE jobs SET status='running' WHERE id=?",(job['id'],))
                                runtime=TaskRuntime(lease,IdentityPool(root/('pool-'+node),start=670000,size=4))
                                cached=store.project_dir(project)/relative
                                self.assertEqual(cached.stat().st_uid,0)
                                gateway=Gateway(runtime.path/'gateway.sock',runtime.project,store.assets(project),
                                    'execute','test','','',runtime.uid,runtime.gid,lambda *_:None,
                                    lambda *_:self.fail('Cached transcripts must not decode audio'))
                                gateway.start()
                                def request(body):
                                    client=http.client.HTTPConnection('localhost',timeout=20)
                                    client.sock=socket.socket(socket.AF_UNIX);client.sock.settimeout(20)
                                    client.sock.connect(str(gateway.socket_path))
                                    try:
                                        client.request('POST','/transcribe',json.dumps(body),{'Content-Type':'application/json'})
                                        response=client.getresponse();return response.status,json.loads(response.read())
                                    finally:client.close()
                                status,body=request({'asset':'speech','audio_track':1})
                                self.assertEqual(status,200,body);self.assertEqual(body['transcript'],transcript)
                                self.assertEqual(cached.stat().st_uid,0)
                                self.assertEqual(cached.stat().st_mode&0o777,0o640)
                                staged=runtime.transcripts/cached.name
                                self.assertEqual(json.loads(staged.read_text()),transcript)
                                self.assertEqual(staged.stat().st_uid,runtime.uid)
                                self.assertEqual(staged.stat().st_mode&0o777,0o600)
                                self.assertFalse(store.query('SELECT * FROM workflow_provider_calls WHERE workflow=?',(job['id'],)))
                                self.assertEqual(request({'asset':'foreign','audio_track':1})[0],404)
                                # Deny read access after startup to test the actual
                                # low-privilege HTTP path, without a fake exception.
                                mode=cached.parent.stat().st_mode&0o777
                                cached.parent.chmod(0o000)
                                try:
                                    status,body=request({'asset':'speech','audio_track':1})
                                    self.assertEqual(status,500,body)
                                    self.assertEqual(body['error']['code'],'filesystem_permission_denied')
                                    self.assertNotIn(str(root),json.dumps(body))
                                finally:cached.parent.chmod(mode)
                                store.execute("UPDATE jobs SET status='succeeded' WHERE id=?",(job['id'],))
                        finally:
                            if gateway:gateway.close()
                            if runtime:runtime.close()
                            lease.release()
                        # Recovery must work with no access to the prior cache.
                        shutil.rmtree(store.project_dir(project))
                restored=origin.artifacts.load_file(project,relative)
                self.assertEqual(json.loads(restored.read_text()),transcript)
        finally:s3.close();pg.close()

    def test_canvas_result_stored_encrypted_and_readable_on_second_api_node(self):
        pg=PostgresServer();s3=S3Server()
        try:
            with tempfile.TemporaryDirectory(prefix='vg-') as directory,patch.dict(os.environ,{
                'VIDEO_USE_DATABASE_URL':pg.dsn,'VIDEO_USE_DISTRIBUTED':'1','VIDEO_USE_OBJECT_BUCKET':'gateway-files',
                'VIDEO_USE_OBJECT_ENDPOINT':s3.endpoint,'AWS_ACCESS_KEY_ID':'test','AWS_SECRET_ACCESS_KEY':'test',
                'AWS_DEFAULT_REGION':'us-east-1','VIDEO_USE_OBJECT_ENCRYPTION_KEY':base64.b64encode(b'x'*32).decode(),
                'VIDEO_USE_OBJECT_REQUIRE_ENCRYPTION':'1','VIDEO_USE_SESSION_SECRET':'ab'*32,
                'VIDEO_USE_TASK_MEMORY_BYTES':str(512*1024**2),'VIDEO_USE_TASK_DISK_BYTES':str(64*1024**2)}):
                from webapp.app import create_app
                from webapp.store import Store
                from webapp import agent_store,agent_sandbox
                from webapp.artifact_store import S3Objects
                from webapp.image_canvas import write_version,bind_context,agent_patch
                from webapp.image_document import Document,Layer
                from webapp.execution_resources import IdentityPool
                from webapp.execution_runtime import TaskRuntime
                from webapp.task_leases import Coordinator
                from webapp.agent_gateway import Gateway
                from webapp.workflow_review import dispatch
                root=Path(directory);backend=S3Objects('gateway-files',s3.endpoint);backend.client.create_bucket(Bucket='gateway-files')
                app=create_app(root/'a',run_worker=False);store=app.state.store
                project=store.add_project('gateway','encrypted canvas')['id'];source=store.project_dir(project)/'sources/source.png'
                Image.new('RGB',(16,16),'blue').save(source)
                store.add_asset(project,'source','source','source.png',{'kind':'image','width':16,'height':16,'size':source.stat().st_size})
                document=Document(revision=1,width=16,height=16,layers=[Layer(id='photo',kind='image',asset='source',width=16,height=16)])
                with store.connect() as db:write_version(db,project,document,{'kind':'test'})
                context=bind_context(store,project,{'revision':document.revision,'layer_id':'photo'})
                plan=agent_store.add_turn(store,project,'plan','green');agent_store.finish(store,plan,'succeeded','green image approved')
                task=agent_store.add_turn(store,project,'execute','green',plan_id=plan,canvas_context=context)
                lease=Coordinator(store,'gateway-node','test').claim(task,'agent');runtime=None;gateway=None
                try:
                    with lease.bind():
                        store.execute("UPDATE agent_turns SET status='running' WHERE id=?",(task,))
                        runtime=TaskRuntime(lease,IdentityPool(root/'node',start=670000,size=4))
                        env=agent_sandbox.prepare(store,project,'test','execute',task)
                        gateway=Gateway(runtime.path/'gateway.sock',env['project'],env['assets'],'execute','test','','',runtime.uid,runtime.gid,
                                        lambda *_:None,lambda *_:None,env['workspace'],canvas_callback=lambda body,asset:agent_patch(store,project,task,body,asset),
                                        workflow_callback=lambda body:dispatch(store,task,body,'execute'))
                        gateway.start()
                        client=http.client.HTTPConnection('localhost',timeout=20);client.sock=socket.socket(socket.AF_UNIX);client.sock.settimeout(20);client.sock.connect(str(gateway.socket_path))
                        client.request('POST','/workflow',json.dumps({'action':'preflight','plan':{'unknown':'PRIVATE rejected input'}}),{'Content-Type':'application/json'})
                        response=client.getresponse();body=json.loads(response.read());client.close()
                        self.assertEqual(response.status,422,body)
                        self.assertIn('unknown: extra_forbidden',body['error'])
                        self.assertNotIn('PRIVATE',json.dumps(body));self.assertNotIn('权限',body['error'])
                        # Graph checks raise ordinary ValueError after schema
                        # validation. They must not become misleading lease errors.
                        from tests.test_workflows import local_step
                        client=http.client.HTTPConnection('localhost',timeout=20);client.sock=socket.socket(socket.AF_UNIX);client.sock.settimeout(20);client.sock.connect(str(gateway.socket_path))
                        client.request('POST','/workflow',json.dumps({'action':'preflight','plan':{
                            'key':'invalid-graph','name':'Invalid graph','steps':[local_step('same'),local_step('same')]}}),{'Content-Type':'application/json'})
                        response=client.getresponse();body=json.loads(response.read());client.close()
                        self.assertEqual(response.status,422,body);self.assertIn('校验',body['error'])
                        self.assertNotIn('权限已失效',body['error'])
                        result=subprocess.run(agent_sandbox.command(env,gateway.socket_path,['python','-c',
                            "import os; assert 'AWS_SECRET_ACCESS_KEY' not in os.environ; assert 'VIDEO_USE_OBJECT_ENCRYPTION_KEY' not in os.environ"]),capture_output=True,timeout=20)
                        self.assertEqual(result.returncode,0,result.stderr)
                        # Exercise the deployed read-only quality checker inside the
                        # real namespace, with the shared format module mounted.
                        from tests.test_workflow_reliability import ASS
                        subtitle=env['workspace']/'edit'/'overlay.ass';subtitle.write_text(ASS)
                        os.chown(subtitle,runtime.uid,runtime.gid)
                        checked=subprocess.run(agent_sandbox.command(dict(env,mode='plan'),gateway.socket_path,
                            ['python','/opt/quality_check.py']),input=json.dumps({'files':['edit/overlay.ass'],
                            'acceptance':[{'kind':'text','file':'overlay.ass','contains':['你好']},
                                          {'kind':'subtitle','file':'overlay.ass'}]}).encode(),capture_output=True,timeout=30)
                        self.assertEqual(checked.returncode,0,checked.stderr);self.assertTrue(json.loads(checked.stdout)['passed'])
                        readonly=json.loads((env['workspace']/'continuation.json').read_text()) if (env['workspace']/'continuation.json').stat().st_size else json.loads((runtime.agent/'continuation.json').read_text())
                        self.assertFalse(readonly['execution_authorized'])
                        raw=io.BytesIO();Image.new('RGB',(16,16),'green').save(raw,'PNG')
                        client=http.client.HTTPConnection('localhost',timeout=20);client.sock=socket.socket(socket.AF_UNIX);client.sock.settimeout(20);client.sock.connect(str(gateway.socket_path))
                        client.request('POST','/canvas-edit',json.dumps({'apply':True,'patch':{},'image':base64.b64encode(raw.getvalue()).decode()}),{'Content-Type':'application/json'})
                        response=client.getresponse();body=json.loads(response.read());client.close()
                        self.assertEqual(response.status,200,body);self.assertTrue(body['applied'])
                        new_id=body['after']['layers'][0]['asset']
                    b=Store(root/'b');asset=next(a for a in b.assets(project) if a['id']==new_id)
                    restored=b.file(project,'sources/'+asset['filename'])
                    self.assertEqual(Image.open(restored).convert('RGB').getpixel((0,0)),(0,128,0))
                finally:
                    if gateway:gateway.close()
                    if runtime:runtime.close()
                    lease.release()
        finally:s3.close();pg.close()
