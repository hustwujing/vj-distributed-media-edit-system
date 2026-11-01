import base64
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest
from unittest.mock import patch
from PIL import Image
from fastapi.testclient import TestClient
from tests.distributed_support import PostgresServer,S3Server
from webapp import agent_store
from webapp.demo_library import DemoLibrary,OWNER
from webapp.store import Store
from webapp.artifact_store import S3Objects
from webapp.app import create_app


def source_library(root):
    (root/'sources').mkdir(parents=True);(root/'tiny').mkdir()
    Image.new('RGB',(16,16),'blue').save(root/'tiny/poster.png')
    subprocess.run(['ffmpeg','-v','error','-y','-f','lavfi','-i','color=blue:s=32x32:r=10','-t','0.5','-c:v','libx264','-threads','1',str(root/'sources/source.mp4')],check=True)
    (root/'sources/words.json').write_text(json.dumps({'words':[]}))
    plan={'version':1,'sources':{'v':'sources/source.mp4'},'transcripts':{'v':'transcripts/words.json'},'clips':[{'source':'v','end':.4}]}
    (root/'tiny/plan.json').write_text(json.dumps(plan))
    catalog=[{'id':'tiny','title':'Test','poster':'poster.png','files':['poster.png','plan.json'],'renderer':'edit_media','workflow':'agent','prompt':'查看示例方案',
              'sources':[{'key':'v','file':'source.mp4','name':'Source','transcripts':{'0':'words.json'}}], 'preset':{'kind':'agent','assets':['v']}}]
    (root/'catalog.json').write_text(json.dumps(catalog));return catalog


class LibraryTests(unittest.TestCase):
    def test_atomic_editions_reuse_objects_and_reject_unlisted_files(self):
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder);source=root/'source';catalog=source_library(source);store=Store(root/'node');agent_store.initialize(store);library=DemoLibrary(store)
            first=library.publish(source);old=library.snapshot();objects=len(store.query('SELECT id FROM artifact_objects'))
            self.assertFalse(library.publish(source)['changed']);self.assertEqual(objects,len(store.query('SELECT id FROM artifact_objects')))
            (source/'tiny/poster.png').unlink()
            with self.assertRaises(FileNotFoundError):library.publish(source)
            self.assertEqual(library.snapshot().record['version'],first['version'])
            self.assertTrue(old.file('tiny/poster.png').is_file())
            Image.new('RGB',(16,16),'red').save(source/'tiny/poster.png')
            with patch.object(store.artifacts,'upload',side_effect=OSError('upload interrupted')),self.assertRaises(OSError):library.publish(source)
            self.assertEqual(library.snapshot().record['version'],first['version'])
            second=library.publish(source)
            self.assertNotEqual(second['version'],first['version']);self.assertEqual(len(store.query('SELECT id FROM artifact_objects')),objects+1)
            self.assertNotEqual(old.file('tiny/poster.png').read_bytes(),library.snapshot().file('tiny/poster.png').read_bytes())
            with self.assertRaises(FileNotFoundError):library.snapshot().file('../private.env')
            with self.assertRaises(FileNotFoundError):library.snapshot().file('tiny/private.env')
    def test_shared_library_and_loaded_plan_survive_loss_of_original_node(self):
        pg=PostgresServer();s3=S3Server()
        try:
            with tempfile.TemporaryDirectory() as folder,patch.dict(os.environ,{'VIDEO_USE_DATABASE_URL':pg.dsn,'VIDEO_USE_OBJECT_ENDPOINT':s3.endpoint,'VIDEO_USE_OBJECT_BUCKET':'demo-qa','AWS_ACCESS_KEY_ID':'test','AWS_SECRET_ACCESS_KEY':'test','AWS_DEFAULT_REGION':'us-east-1','VIDEO_USE_SESSION_SECRET':'b2'*32,'VIDEO_USE_DISTRIBUTED':'1',
                 'VIDEO_USE_OBJECT_ENCRYPTION_KEY':base64.b64encode(b'test encryption key 32 bytes!!!!').decode(),'VIDEO_USE_OBJECT_REQUIRE_ENCRYPTION':'1'}):
                root=Path(folder);source=root/'source';catalog=source_library(source);backend=S3Objects('demo-qa',s3.endpoint);backend.client.create_bucket(Bucket='demo-qa')
                app_a=create_app(root/'a',False);app_b=create_app(root/'b',False);library=DemoLibrary(app_a.state.store);library.publish(source)
                with TestClient(app_a) as a,TestClient(app_b) as b,TestClient(app_b) as stranger:
                    self.assertEqual(a.get('/api/demos').json(),catalog);self.assertEqual(b.get('/api/demos').json(),catalog)
                    self.assertEqual(b.get('/api/demos/tiny/files/poster.png').content,(source/'tiny/poster.png').read_bytes())
                    self.assertEqual(b.get('/api/demos/tiny/files/private.env').status_code,404)
                    response=a.post('/api/demos/tiny/load');self.assertEqual(response.status_code,201,response.text);project=response.json()['project']['id'];b.cookies.update(a.cookies)
                    assets=a.get('/api/projects/'+project).json()['assets'];asset=assets[0]['id']
                    shutil.rmtree(root/'a');shutil.rmtree(source)
                    self.assertEqual(b.get('/api/demos').json(),catalog)
                    self.assertEqual(b.get('/api/demos/tiny/sources/v').status_code,200)
                    self.assertEqual(b.get('/api/projects/'+project).status_code,200);self.assertEqual(stranger.get('/api/projects/'+project).status_code,404)
                    store=app_b.state.store;store.restore_files(project,'agent/workspace');store.restore_files(project,'edit/transcripts')
                    plan=json.loads(store.file(project,'agent/workspace/edit/demo-plan.json').read_text())
                    self.assertIn(asset,plan['sources']['v']);self.assertTrue(store.file(project,'edit/transcripts/'+asset+'.json').is_file())
                    self.assertTrue(store.file(project,plan['sources']['v']).is_file())
                    from webapp.task_leases import Coordinator
                    from webapp.execution_resources import IdentityPool
                    from webapp.execution_runtime import TaskRuntime
                    from webapp.plan_readiness import fingerprint
                    original_fingerprint=fingerprint(store,project)
                    (store.project_dir(project)/'agent/workspace/edit/unreferenced-cache.txt').write_text('not an authoritative project file')
                    task=agent_store.add_turn(store,project,'plan','read seeded example');coordinator=Coordinator(store,'demo-node-b','test');lease=coordinator.claim(task,'agent');runtime=None
                    try:
                        with lease.bind():
                            store.execute("UPDATE agent_turns SET status='running' WHERE id=?",(task,))
                            runtime=TaskRuntime(lease,IdentityPool(root/'executor-b',start=630000,size=4));lease.runtime=runtime
                            self.assertEqual(json.loads((runtime.workspace/'edit/demo-plan.json').read_text()),plan)
                            self.assertTrue((runtime.transcripts/(asset+'.json')).is_file())
                            self.assertFalse((runtime.workspace/'edit/unreferenced-cache.txt').exists())
                            runtime.quiesce();runtime.checkpoint()
                            self.assertEqual(fingerprint(store,project),original_fingerprint)
                            self.assertEqual(fingerprint(app_a.state.store,project),original_fingerprint)
                    finally:
                        if runtime:runtime.close()
                        lease.release()

        finally:s3.close();pg.close()
