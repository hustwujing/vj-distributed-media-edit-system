"""Parallel branch ownership and isolated output publication on real shared state."""
from concurrent.futures import ThreadPoolExecutor
import json
import multiprocessing
import os
from pathlib import Path
import shutil
import socket
import subprocess
import tempfile
import threading
import unittest
from unittest.mock import patch

from tests.distributed_support import PostgresServer,S3Server
from webapp import agent_store,workflows,agent_sandbox
from webapp.artifact_store import S3Objects
from webapp.execution_resources import IdentityPool
from webapp.execution_runtime import TaskRuntime
from webapp.store import Store
from webapp.task_leases import Coordinator
from webapp.workflow_runtime import Adapter


def crash_branch(root,task,channel):
    root=Path(root);store=Store(root/'crashed-data')
    coordinator=Coordinator(store,'crashed-node','test',ttl=60)
    lease=coordinator.claim(task,'workflow',step_id='left')
    with lease.bind():
        workflow=workflows.get(store,task)
        workflows.change(store,workflow['id'],'left',status='running',attempts=1)
        runtime=TaskRuntime(lease,IdentityPool(root/'crashed-node',start=650000,size=4))
        env=agent_sandbox.prepare(store,workflow['project'],'test','execute',task)
        path=runtime.path/'live.sock'
        with socket.socket(socket.AF_UNIX) as sock:
            sock.bind(str(path));os.chown(path,runtime.uid,runtime.gid);path.chmod(0o600)
            process=subprocess.Popen(agent_sandbox.command(env,path,['python','-c','import time;time.sleep(60)']),
                                     stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
            channel.send(lease.attempt)
            process.wait()


@unittest.skipUnless(os.geteuid()==0 and shutil.which('bwrap'),'Requires privileged sandbox prerequisites')
class DistributedWorkflowTests(unittest.TestCase):
    def test_parallel_branches_recover_killed_node_and_preserve_outputs_for_join(self):
        pg=PostgresServer();s3=S3Server()
        try:
            with tempfile.TemporaryDirectory(prefix='vu-parallel-') as folder,patch.dict(os.environ,{
                'VIDEO_USE_DATABASE_URL':pg.dsn,'VIDEO_USE_OBJECT_ENDPOINT':s3.endpoint,'VIDEO_USE_OBJECT_BUCKET':'parallel',
                'AWS_ACCESS_KEY_ID':'test','AWS_SECRET_ACCESS_KEY':'test','AWS_DEFAULT_REGION':'us-east-1',
                'VIDEO_USE_DISTRIBUTED':'1','VIDEO_USE_TASK_DISK_BYTES':str(64*1024**2),
                'VIDEO_USE_TASK_MEMORY_BYTES':str(256*1024**2),'VIDEO_USE_NODE_CPU_SLOTS':'8',
            }):
                root=Path(folder);backend=S3Objects('parallel',s3.endpoint);backend.client.create_bucket(Bucket='parallel')
                stores=[Store(root/name) for name in ('a','b','c')]
                agent_store.initialize(stores[0]);project=stores[0].add_project('parallel','project')['id']
                plan=agent_store.add_turn(stores[0],project,'plan','parallel branches')
                agent_store.finish(stores[0],plan,'succeeded','approved')
                task=agent_store.add_turn(stores[0],project,'execute','execute',plan_id=plan)
                coordinators=[Coordinator(store,'parallel-'+str(i),'test',ttl=60) for i,store in enumerate(stores)]
                pools=[IdentityPool(root/('node-'+str(i)),start=630000+i*100,size=4) for i in range(3)]
                seed=coordinators[0].claim(task,'agent')
                runtime=None
                try:
                    with seed.bind():
                        stores[0].execute("UPDATE agent_turns SET status='running' WHERE id=?",(task,))
                        runtime=TaskRuntime(seed,pools[0]);env=agent_sandbox.prepare(stores[0],project,'test','execute',task)
                        script=env['workspace']/'edit/branch.py'
                        script.write_text("import os,sys,json\nfrom pathlib import Path\ninputs=json.loads(os.environ.get('VIDEO_USE_STEP_INPUTS','{}'))\ntext=sys.argv[1]\nif sys.argv[1]=='join':text+=':'+':'.join(Path(inputs[role][0]).read_text() for role in ('left','right'))\nPath(os.environ['VIDEO_USE_STEP_OUTPUT'],'result.txt').write_text(text)\n")
                        os.chown(script,runtime.uid,runtime.gid)
                        runtime.checkpoint()
                finally:
                    if runtime:runtime.close()
                    seed.release()
                def step(name,dependencies=()):
                    return {'id':name,'name':name,'kind':'script','argv':['python','edit/branch.py',name],
                            'outputs':['result.txt'],'depends_on':list(dependencies),'retry_safe':True,
                            'output_roles':{'text':'result.txt'},
                            'inputs':{dep:{'step':dep,'role':'text'} for dep in dependencies},
                            'goal':'write branch marker','acceptance':[{'kind':'text','contains':['join:left:right' if name=='join' else name]}],'on_failure':'pause'}
                workflows.submit(stores[0],task,{'key':'parallel','name':'parallel','steps':[step('left'),step('right'),step('join',('left','right'))]})
                workflows.state(stores[0],task,'ready')
                ctx=multiprocessing.get_context('spawn');receiver,sender=ctx.Pipe(duplex=False)
                crashed=ctx.Process(target=crash_branch,args=(str(root),task,sender))
                try:
                    crashed.start();self.assertTrue(receiver.poll(20),'Failed to start crash fixture')
                    old_attempt=receiver.recv()
                finally:
                    if crashed.is_alive():crashed.kill()
                    crashed.join(timeout=5)
                stores[0].execute('UPDATE execution_leases SET expires=0 WHERE attempt=?',(old_attempt,))
                # Model the original node's independent reaper, then remove all
                # its data before another executor takes over the step.
                IdentityPool(root/'crashed-node',start=650000,size=4)
                shutil.rmtree(root/'crashed-data')
                leases=[coordinators[i].claim(task,'workflow',per_user=3,step_id=name) for i,name in enumerate(('left','right'))]
                self.assertTrue(all(leases))
                self.assertTrue(leases[0].recovered)
                self.assertIsNone(coordinators[2].claim(task,'workflow',per_user=3,step_id='left'))
                self.assertIsNone(coordinators[2].claim(task,'workflow',per_user=3,step_id='join'))
                barrier=threading.Barrier(2)
                def execute(index,lease):
                    lease.runtime_factory=lambda:TaskRuntime(lease,pools[index])
                    socket_dir=root/('sockets-'+str(index));socket_dir.mkdir(exist_ok=True)
                    runner=workflows.Runner(stores[index],Adapter(stores[index],'test',socket_dir),threading.Event())
                    try:
                        with lease.bind():
                            if lease.recovered:runner.recover(task,step_id=lease.step_id)
                            barrier.wait(timeout=10)
                            runner.tick(task,step_id=lease.step_id)
                    finally:
                        if lease.runtime:lease.runtime.close()
                        lease.release()
                with ThreadPoolExecutor(max_workers=2) as pool:
                    futures=[pool.submit(execute,i,lease) for i,lease in enumerate(leases)]
                    for future in futures:future.result(timeout=40)
                state=workflows.get(stores[2],task)
                self.assertEqual(state['status'],'ready',{'error':state['error'],'diagnostics':stores[2].query('SELECT * FROM workflow_diagnostics')})
                self.assertEqual([s['status'] for s in state['steps']],['succeeded','succeeded','pending'],state)
                self.assertEqual(state['steps'][0]['attempts'],2)
                self.assertEqual(len(stores[2].query('SELECT * FROM execution_step_checkpoints WHERE workflow=?',(state['id'],))),2)
                shutil.rmtree(stores[0].root);shutil.rmtree(stores[1].root)
                join=coordinators[2].claim(task,'workflow',per_user=3,step_id='join')
                self.assertIsNotNone(join,{'workflow':workflows.get(stores[2],task)['status'],
                    'leases':stores[2].query('SELECT slot,active,expires FROM execution_leases'),
                    'waits':stores[2].query('SELECT * FROM execution_waits'),
                    'limits':stores[2].query('SELECT * FROM execution_limits')})
                restored=None
                try:
                    with join.bind():
                        restored=TaskRuntime(join,pools[2])
                        for s in state['steps'][:2]:
                            self.assertEqual((restored.workspace/s['result'][0]).read_text(),s['id'])
                        sockets=root/'join-sockets';sockets.mkdir()
                        adapter=Adapter(stores[2],'test',sockets)
                        runner=workflows.Runner(stores[2],adapter,threading.Event())
                        runner.tick(task,step_id='join')
                        result=workflows.get(stores[2],task)['steps'][2]
                        self.assertEqual(result['status'],'succeeded',result['diagnostics'])
                        self.assertEqual((restored.workspace/result['result'][0]).read_text(),'join:left:right')
                        self.assertEqual(result['quality']['input_versions'],{'left':0,'right':0})
                finally:
                    if restored:restored.close()
                    join.release()
                # The project checkpoint predates these independent step outputs.
                # A new planning turn on a fresh node must still see them after
                # the old workflow is stopped, without accepting/reexecuting any.
                workflows.cancel(stores[2],task)
                shutil.rmtree(stores[2].root)
                fresh=Store(root/'handoff')
                # The killed node no longer heartbeats. Advance only that fixture
                # worker's liveness marker so unrelated cleanup cannot block handoff.
                fresh.execute("UPDATE execution_workers SET heartbeat=0 WHERE node='crashed-node'")
                next_task=agent_store.add_turn(fresh,project,'plan','inspect retained results')
                next_lease=Coordinator(fresh,'handoff-node','test').claim(next_task,'agent')
                self.assertIsNotNone(next_lease,{'waits':fresh.query('SELECT * FROM execution_waits WHERE task_id=?',(next_task,)), 'cleanup':fresh.query("SELECT task_id,node,state FROM execution_cleanup WHERE state<>'complete'")})
                next_runtime=None
                try:
                    with next_lease.bind():
                        next_runtime=TaskRuntime(next_lease,pools[2])
                        self.assertEqual((next_runtime.workspace/result['result'][0]).read_text(),'join:left:right')
                        for saved in state['steps'][:2]:
                            self.assertEqual((next_runtime.workspace/saved['result'][0]).read_text(),saved['id'])
                        self.assertEqual(workflows.get(fresh,task)['status'],'cancelled')
                        self.assertEqual([s['attempts'] for s in workflows.get(fresh,task)['steps']],[2,1,1])
                finally:
                    if next_runtime:next_runtime.close()
                    next_lease.release()
        finally:s3.close();pg.close()


if __name__=='__main__':unittest.main()
