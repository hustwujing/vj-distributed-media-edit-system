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
                                        lambda *_:None,lambda *_:None,env['workspace'],canvas_callback=lambda body,asset:agent_patch(store,project,task,body,asset))
                        gateway.start()
                        result=subprocess.run(agent_sandbox.command(env,gateway.socket_path,['python','-c',
                            "import os; assert 'AWS_SECRET_ACCESS_KEY' not in os.environ; assert 'VIDEO_USE_OBJECT_ENCRYPTION_KEY' not in os.environ"]),capture_output=True,timeout=20)
                        self.assertEqual(result.returncode,0,result.stderr)
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
