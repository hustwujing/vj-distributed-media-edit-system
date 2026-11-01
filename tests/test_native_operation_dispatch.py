import os
from pathlib import Path
import shutil
import tempfile
import unittest
from unittest.mock import patch
from PIL import Image,ImageDraw
from webapp import agent_store,workflows
from webapp.store import Store
from webapp.agent_host import AgentHost
from webapp.execution_resources import IdentityPool
from webapp.execution_runtime import TaskRuntime

@unittest.skipUnless(os.geteuid()==0 and shutil.which('bwrap'),'Privileged runtime required')
class NativeDispatchTests(unittest.TestCase):
    @unittest.skipUnless(os.environ.get('VIDEO_USE_TEST_REAL_MODELS')=='1','Explicit real GPU workflow dispatch smoke')
    def test_real_cutout_uses_pinned_step_resources_after_claim(self):
        with tempfile.TemporaryDirectory(prefix='vg-') as directory,patch.dict(os.environ,{'VIDEO_USE_TASK_MEMORY_BYTES':str(8*1024**3),'VIDEO_USE_TASK_DISK_BYTES':str(64*1024**2)}):
            root=Path(directory);store=Store(root/'data');agent_store.initialize(store);project=store.add_project('owner','dispatch')['id']
            path=store.project_dir(project)/'sources/logo.png'
            image=Image.new('RGB',(256,256),'white');ImageDraw.Draw(image).ellipse((40,40,216,216),fill='blue');image.save(path)
            store.add_asset(project,'logo','logo','logo.png',{'kind':'image','width':256,'height':256})
            plan=agent_store.add_turn(store,project,'plan','cutout');agent_store.finish(store,plan,'succeeded','approved')
            turn=agent_store.add_turn(store,project,'execute','execute',plan_id=plan);store.execute("UPDATE agent_turns SET status='running' WHERE id=?",(turn,))
            workflows.submit(store,turn,{'key':'dispatch','name':'dispatch','steps':[{'id':'cutout','name':'cutout','kind':'operation','operation':'image.remove_background',
                'inputs':{'source':{'asset':'logo'}},'goal':'extract foreground','on_failure':'pause','acceptance':[{'kind':'image','file':'foreground.png','has_alpha':True},{'kind':'image','file':'mask.png','width':256}]}]})
            workflows.state(store,turn,'ready');host=AgentHost(store.root);host.identities=IdentityPool(root/'node',start=600000,size=4)
            lease=host.coordinator.claim(turn,'workflow');self.assertEqual(lease.step_id,'cutout')
            observed=[]
            def create(lease,pool):
                runtime=TaskRuntime(lease,pool)
                for group in runtime.groups.paths:
                    for name in ('memory.max','memory.limit_in_bytes'):
                        if (group/name).exists():observed.append(int((group/name).read_text()))
                return runtime
            with patch('webapp.agent_host.TaskRuntime',side_effect=create):host.execute_claim(lease)
            w=workflows.get(store,turn)
            self.assertEqual(w['steps'][0]['status'],'succeeded',w['steps'][0]['diagnostics'])
            self.assertTrue(observed);self.assertGreaterEqual(observed[0],8*1024**3)
            attempt=store.query('SELECT permission_profile,step_id FROM execution_attempts WHERE id=?',(lease.attempt,))[0]
            self.assertEqual(attempt,{'permission_profile':'fixed_tool','step_id':'cutout'})
