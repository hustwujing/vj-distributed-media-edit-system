"""Real sandbox checkpoints restored on a node with no original local files."""
import json
import os
from pathlib import Path
import shutil
import socket
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from tests.distributed_support import PostgresServer,S3Server
from webapp import agent_store,agent_sandbox
from webapp.artifact_store import S3Objects
from webapp.execution_resources import IdentityPool
from webapp.execution_runtime import TaskRuntime
from webapp.store import Store
from webapp.task_leases import Coordinator


@unittest.skipUnless(os.geteuid()==0 and shutil.which('bwrap'),'Requires privileged sandbox prerequisites')
class DistributedRuntimeTests(unittest.TestCase):
    def test_remote_checkpoint_survives_loss_of_first_node(self):
        pg=PostgresServer();s3=S3Server()
        try:
            with tempfile.TemporaryDirectory(prefix='vu-cross-node-') as folder,patch.dict(os.environ,{
                'VIDEO_USE_DATABASE_URL':pg.dsn,'VIDEO_USE_OBJECT_ENDPOINT':s3.endpoint,
                'VIDEO_USE_OBJECT_BUCKET':'cross-node','AWS_ACCESS_KEY_ID':'test','AWS_SECRET_ACCESS_KEY':'test',
                'AWS_DEFAULT_REGION':'us-east-1','VIDEO_USE_DISTRIBUTED':'1','VIDEO_USE_TASK_DISK_BYTES':str(64*1024**2),
            }):
                root=Path(folder)
                backend=S3Objects('cross-node',s3.endpoint)
                backend.client.create_bucket(Bucket='cross-node')
                a=Store(root/'a');agent_store.initialize(a)
                project=a.add_project('cross-node','project')['id']
                task=agent_store.add_turn(a,project,'plan','checkpoint')
                ca=Coordinator(a,'node-a','test',ttl=60)
                first=ca.claim(task,'agent')
                runtime=None
                try:
                    with first.bind():
                        a.execute("UPDATE agent_turns SET status='running' WHERE id=?",(task,))
                        runtime=TaskRuntime(first,IdentityPool(root/'node-a',start=610000,size=4))
                        env=agent_sandbox.prepare(a,project,'test','execute',task)
                        with socket.socket(socket.AF_UNIX) as sock:
                            path=runtime.path/'test.sock';sock.bind(str(path));os.chown(path,runtime.uid,runtime.gid);path.chmod(0o600)
                            result=subprocess.run(agent_sandbox.command(env,path,['python','-c',
                                "from pathlib import Path; Path('edit/shared.txt').write_text('node-a durable output')"]),
                                capture_output=True,text=True,timeout=20)
                            self.assertEqual(result.returncode,0,result.stderr)
                        runtime.checkpoint()
                finally:
                    if runtime:runtime.close()
                    first.release(interrupted=True)
                record=a.query('SELECT manifest FROM execution_checkpoints WHERE project=?',(project,))[0]
                self.assertIn('artifact',json.loads(record['manifest'])['workspace']['edit/shared.txt'])
                shutil.rmtree(a.root)
                b=Store(root/'b')
                cb=Coordinator(b,'node-b','test',ttl=60)
                second=cb.claim(task,'agent')
                restored=None
                try:
                    with second.bind():
                        restored=TaskRuntime(second,IdentityPool(root/'node-b',start=620000,size=4))
                        self.assertEqual((restored.workspace/'edit/shared.txt').read_text(),'node-a durable output')
                        from webapp.agent_gateway import Gateway
                        env=agent_sandbox.prepare(b,project,'test','execute',task)
                        gateway=Gateway(restored.path/'broker.sock',env['project'],env['assets'],'execute','test','','',restored.uid,restored.gid,lambda *_:None,lambda *_:None,env['workspace'])
                        try:
                            self.assertFalse((b.project_dir(project)/'edit/transcripts').exists())
                            gateway.start()
                            self.assertTrue((b.project_dir(project)/'edit/transcripts').is_dir())
                            self.assertTrue((b.project_dir(project)/'sources').is_dir())
                        finally:gateway.close()
                        agent_store.finish(b,task,'succeeded','restored')
                finally:
                    if restored:restored.close()
                    second.release()
        finally:s3.close();pg.close()


if __name__=='__main__':unittest.main()
