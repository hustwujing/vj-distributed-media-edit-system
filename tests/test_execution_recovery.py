"""Kill a real executor process, then recover its isolated workflow on another instance."""
import multiprocessing
import json
import os
from pathlib import Path
import shutil
import socket
import subprocess
import tempfile
import threading
import unittest
from unittest.mock import patch

from webapp import agent_store, agent_sandbox, workflows
from webapp.store import Store
from webapp.task_leases import Coordinator
from webapp.execution_resources import IdentityPool,uid_alive
from webapp.execution_runtime import TaskRuntime


def crashable_worker(root, turn, pipe):
    store=Store(Path(root))
    coordinator=Coordinator(store,'old-node','local-v1')
    lease=coordinator.claim(turn,'workflow')
    with lease.bind():
        workflow=workflows.get(store,turn)
        workflows.change(store,workflow['id'],'render',status='running',attempts=1)
        runtime=TaskRuntime(lease,IdentityPool())
        lease.runtime=runtime
        env=agent_sandbox.prepare(store,workflow['project'],'test','execute',turn)
        path=runtime.path/'test.sock'
        sock=socket.socket(socket.AF_UNIX);sock.bind(str(path))
        os.chown(path,runtime.uid,runtime.gid);path.chmod(0o600)
        process=subprocess.Popen(agent_sandbox.command(env,path,
            ['/opt/venv/bin/python','-c','import time; time.sleep(600)']),stdout=subprocess.DEVNULL,stderr=subprocess.PIPE)
        pipe.send((lease.attempt,runtime.uid))
        process.wait()


@unittest.skipUnless(os.geteuid()==0 and shutil.which('bwrap'),'Requires Linux execution service privileges')
class RecoveryTests(unittest.TestCase):
    def test_killed_worker_is_replaced_and_safe_step_is_replayed_once(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp)
            with patch.dict(os.environ,{'VIDEO_USE_EXECUTOR_ROOT':str(root/'node'),
                    'VIDEO_USE_UID_START':'460000','VIDEO_USE_UID_COUNT':'4',
                    'VIDEO_USE_TASK_MEMORY_BYTES':str(256*1024**2),'VIDEO_USE_TASK_DISK_BYTES':str(64*1024**2)}):
                store=Store(root/'shared');agent_store.initialize(store)
                project=store.add_project('owner','project')['id']
                plan=agent_store.add_turn(store,project,'plan','plan')
                agent_store.finish(store,plan,'succeeded','approved')
                turn=agent_store.add_turn(store,project,'execute','execute',plan_id=plan)
                store.execute("UPDATE agent_turns SET status='running' WHERE id=?",(turn,))
                env=agent_sandbox.prepare(store,project,'test','execute',turn)
                script=env['workspace']/'edit/render.py'
                script.write_text("import os\nfrom pathlib import Path\nPath(os.environ['VIDEO_USE_STEP_OUTPUT'],'result.txt').write_text('recovered')\n")
                script.chmod(0o644)
                workflows.submit(store,turn,{'key':'recovery','name':'Recovery test','steps':[
                    {'id':'render','name':'Render','kind':'script','argv':['python','edit/render.py'],
                     'outputs':['result.txt'],'retry_safe':True,'goal':'Produce recovered text','on_failure':'pause',
                     'acceptance':[{'kind':'text','contains':['recovered']}]} ]})
                workflows.state(store,turn,'ready')
                ctx=multiprocessing.get_context('spawn')
                receiver,sender=ctx.Pipe(duplex=False)
                process=ctx.Process(target=crashable_worker,args=(str(store.root),turn,sender))
                process.start()
                try:
                    self.assertTrue(receiver.poll(30),'Executor did not initialize')
                    old_attempt,uid=receiver.recv()
                finally:
                    process.kill();process.join(timeout=10)
                store.execute('UPDATE execution_leases SET expires=0 WHERE run_id=?',(turn,))
                from webapp.agent_host import AgentHost
                host=AgentHost(store.root)
                lease=host.coordinator.claim(turn,'workflow')
                self.assertTrue(lease.recovered)
                self.assertNotEqual(lease.attempt,old_attempt)
                host.execute_claim(lease)
                result=workflows.get(store,turn)
                self.assertEqual(result['steps'][0]['status'],'succeeded',result)
                self.assertEqual(result['steps'][0]['attempts'],2)
                checkpoint=store.query('SELECT * FROM execution_step_checkpoints WHERE workflow=? AND step=?', (result['id'],'render'))[0]
                artifact=result['steps'][0]['result'][0]
                record=json.loads(checkpoint['manifest'])[artifact]
                self.assertEqual((store.root/record['location']).read_text(),'recovered')
                self.assertFalse(uid_alive(uid))
                self.assertFalse((root/'node/runs'/old_attempt).exists())

    def test_draining_worker_stops_claiming_without_resetting_queued_tasks(self):
        with tempfile.TemporaryDirectory() as tmp, patch.dict(os.environ,{'VIDEO_USE_EXECUTOR_ROOT':tmp+'/node'}):
            from webapp.agent_host import AgentHost
            host=AgentHost(Path(tmp)/'data')
            project=host.store.add_project('owner','project')['id']
            job=host.store.add_job(project,'pack',{'kind':'pack'})
            host.store.execute('UPDATE execution_workers SET draining=1 WHERE id=?',(host.coordinator.worker,))
            thread=threading.Thread(target=host.run)
            thread.start();thread.join(timeout=5)
            self.assertFalse(thread.is_alive())
            self.assertEqual(host.store.job(job['id'])['status'],'queued')

    def test_cancellation_after_claim_never_starts_a_sandbox(self):
        with tempfile.TemporaryDirectory() as tmp,patch.dict(os.environ,{'VIDEO_USE_EXECUTOR_ROOT':tmp+'/node'}):
            from webapp.agent_host import AgentHost
            host=AgentHost(Path(tmp)/'data')
            project=host.store.add_project('owner','project')['id']
            turn=agent_store.add_turn(host.store,project,'plan','plan')
            lease=host.coordinator.claim(turn,'agent')
            host.store.execute("UPDATE agent_turns SET status='cancelled' WHERE id=?",(turn,))
            with patch('webapp.agent_host.TaskRuntime') as runtime:
                host.execute_claim(lease)
                runtime.assert_not_called()
            self.assertEqual(agent_store.turn(host.store,turn)['status'],'cancelled')


if __name__=='__main__':unittest.main()
