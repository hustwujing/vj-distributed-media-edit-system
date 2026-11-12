import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest
from unittest.mock import patch
from webapp import agent_store
from webapp.store import Store
from webapp.task_leases import Coordinator
from webapp.live_activity import current,span

class ActivityTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.store=Store(Path(self.tmp.name));agent_store.initialize(self.store)
        self.p=self.store.add_project('owner','activity')['id'];self.t=agent_store.add_turn(self.store,self.p,'plan','test')
        self.coordinator=Coordinator(self.store,'activity-node','test');self.lease=self.coordinator.claim(self.t,'agent')
        self.store.execute("UPDATE agent_turns SET status='running' WHERE id=?",(self.t,))
    def test_model_phases_are_public_states_without_reasoning_text(self):
        from webapp.task_usage import model_call
        with self.lease.bind(),model_call(self.lease,'test',actor='sandbox_agent') as receipt:
            self.assertEqual((current(self.store,self.t)[0]['phase'],current(self.store,self.t)[0]['label']),
                             ('model_submitted','模型请求已提交，等待首包'))
            receipt.on_activity('first_byte')
            self.assertEqual(current(self.store,self.t)[0]['label'],'已收到首包，正在接收模型响应')
            receipt.accept(json.dumps({'type':'response.reasoning_text.delta','delta':'NEVER-EXPOSE-PRIVATE-REASONING'}))
            self.assertEqual(current(self.store,self.t)[0]['phase'],'analysis')
            receipt.accept(json.dumps({'type':'response.output_text.delta','delta':'公开回复'}))
            self.assertEqual(current(self.store,self.t)[0]['phase'],'responding')
        events=self.store.query("SELECT payload FROM agent_events WHERE run_id=? AND kind='work'",(self.t,))
        self.assertNotIn('NEVER-EXPOSE',json.dumps(events));self.assertEqual(current(self.store,self.t)[0]['status'],'completed')
    def test_actor_tool_cache_survives_reconnect_and_attempt_exit(self):
        with self.lease.bind():agent_store.event(self.store,self.t,'tool',{'id':'code','status':'running','category':'write','title':'编写脚本','started':self.store.now()})
        row=current(self.store,self.t)[0];self.assertEqual((row['actor'],row['phase']),('sandbox_agent','writing'))
        self.lease.release(interrupted=True);self.assertEqual(current(self.store,self.t)[0]['status'],'interrupted')
    def test_serialized_script_null_operation_records_start_finish_and_failure(self):
        from webapp.execution_metrics import measured
        @measured('local')
        def local(_,step):
            running=[x for x in current(self.store,self.t) if x['status']=='running']
            self.assertEqual(len(running),1);self.assertEqual(running[0]['operation'],'script')
            if step['id']=='broken':raise ValueError('bad input')
        with self.lease.bind():
            local(None,{'id':'valid','revision':0,'spec':{'operation':None}})
            with self.assertRaises(ValueError):local(None,{'id':'broken','revision':0,'spec':{'operation':None}})
        rows=current(self.store,self.t);self.assertEqual({x['status'] for x in rows},{'completed','failed'})
        self.assertTrue(all(x['actor']=='workflow_sandbox' for x in rows))
        self.assertEqual(len(self.store.query("SELECT * FROM agent_events WHERE kind='work'")),4)

    def test_host_and_workflow_operations_keep_distinct_actor_and_step(self):
        from webapp.execution_metrics import measured
        @measured('local')
        def local(_,w,step):return current(self.store,self.t)
        @measured('submit')
        def submit(_,w,step):return current(self.store,self.t)
        spec={'id':'one','revision':1,'spec':{'operation':'script'}}
        with self.lease.bind():
            self.assertEqual(local(None,{},spec)[0]['actor'],'workflow_sandbox')
            self.assertEqual(submit(None,{},dict(spec,spec={'operation':'video.generate_from_image'}))[0]['actor'],'host')
        rows=current(self.store,self.t);self.assertTrue(all(r['status']=='completed' for r in rows))

@unittest.skipUnless(os.geteuid()==0 and shutil.which('setfacl'),'Requires isolated service UID permission test')
class RuntimeAccessTests(unittest.TestCase):
    def test_symlinked_runtime_package_is_readable_without_exposing_private_siblings(self):
        import redis
        from deploy.runtime_access import grant
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);root.chmod(0o755);dependencies=root/'dependencies';dependencies.mkdir(mode=0o755)
            package=dependencies/'redis';shutil.copytree(Path(redis.__file__).parent,package,ignore=shutil.ignore_patterns('__pycache__'))
            subprocess.run(['setfacl','-Rb',str(package)],check=True)
            for path in [package,*package.rglob('*')]:path.chmod(0o750 if path.is_dir() else 0o640)
            secret=root/'private.env';secret.write_text('private fixture');secret.chmod(0o600)
            link=root/'.venv';link.symlink_to(dependencies,target_is_directory=True)
            code='try:\n import redis\nexcept ModuleNotFoundError:\n print(0)\nelse:\n print(int(callable(getattr(redis,"Redis",None))))'
            def imported():return subprocess.run(['runuser','-u','video-use','--',__import__('sys').executable,'-S','-c',code],env={**os.environ,'PYTHONPATH':str(dependencies)},capture_output=True,text=True,check=True).stdout.strip()
            self.assertEqual(imported(),'0');grant(link);self.assertEqual(imported(),'1')
            result=subprocess.run(['runuser','-u','video-use','--','test','-r',str(secret)])
            self.assertNotEqual(result.returncode,0)
