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
    def test_parallel_real_script_failures_each_receive_diagnosis_after_restart(self):
        from webapp import workflow_review
        pg=PostgresServer();s3=S3Server()
        try:
            with tempfile.TemporaryDirectory(prefix='vu-reviews-') as folder,patch.dict(os.environ,{
                'VIDEO_USE_DATABASE_URL':pg.dsn,'VIDEO_USE_OBJECT_ENDPOINT':s3.endpoint,'VIDEO_USE_OBJECT_BUCKET':'reviews',
                'AWS_ACCESS_KEY_ID':'test','AWS_SECRET_ACCESS_KEY':'test','AWS_DEFAULT_REGION':'us-east-1',
                'VIDEO_USE_DISTRIBUTED':'1','VIDEO_USE_TASK_DISK_BYTES':str(64*1024**2),
                'VIDEO_USE_TASK_MEMORY_BYTES':str(256*1024**2),'VIDEO_USE_NODE_CPU_SLOTS':'8',
            }):
                root=Path(folder);backend=S3Objects('reviews',s3.endpoint);backend.client.create_bucket(Bucket='reviews')
                stores=[Store(root/name) for name in ('a','b')];agent_store.initialize(stores[0])
                project=stores[0].add_project('review-owner','parallel failures')['id']
                plan=agent_store.add_turn(stores[0],project,'plan','two branches and a join')
                agent_store.finish(stores[0],plan,'succeeded','approved')
                task=agent_store.add_turn(stores[0],project,'execute','execute',plan_id=plan)
                coordinators=[Coordinator(store,'reviews-'+str(i),'test',ttl=120) for i,store in enumerate(stores)]
                pools=[IdentityPool(root/('node-'+str(i)),start=750000+i*100,size=4) for i in range(2)]
                seed=coordinators[0].claim(task,'agent');runtime=None
                try:
                    with seed.bind():
                        stores[0].execute("UPDATE agent_turns SET status='running' WHERE id=?",(task,))
                        runtime=TaskRuntime(seed,pools[0]);env=agent_sandbox.prepare(stores[0],project,'test','execute',task)
                        script=env['workspace']/'edit/branch.py'
                        script.write_text("import os,sys,json\nfrom pathlib import Path\n"
                            "if '--fail' in sys.argv:raise ValueError('invalid branch argument')\n"
                            "inputs=json.loads(os.environ.get('VIDEO_USE_STEP_INPUTS','{}'))\n"
                            "text=sys.argv[1]\n"
                            "if text=='join':text+=':'+':'.join(Path(inputs[r][0]).read_text() for r in ('left','right'))\n"
                            "Path(os.environ['VIDEO_USE_STEP_OUTPUT'],'result.txt').write_text(text)\n")
                        os.chown(script,runtime.uid,runtime.gid);runtime.checkpoint()
                finally:
                    if runtime:runtime.close()
                    seed.release()
                def spec(name,deps=()):
                    return {'id':name,'name':name,'kind':'script','depends_on':list(deps),
                        'argv':['python','edit/branch.py',name]+([] if deps else ['--fail']),
                        'inputs':{dep:{'step':dep,'role':'text'} for dep in deps},
                        'outputs':['result.txt'],'output_roles':{'text':'result.txt'},'retry_safe':False,
                        'goal':'Produce branch output','acceptance':[{'kind':'text','contains':[name]}],'on_failure':'diagnose'}
                workflows.submit(stores[0],task,{'key':'reviews','name':'parallel reviews','steps':[spec('left'),spec('right'),spec('join',('left','right'))]})
                workflows.state(stores[0],task,'ready');barrier=threading.Barrier(2)
                class ConcurrentFailureAdapter(Adapter):
                    def local(self,w,step,cancelled):
                        barrier.wait(timeout=20)
                        return super().local(w,step,cancelled)
                def execute(index,lease,parallel=False):
                    lease.runtime_factory=lambda:TaskRuntime(lease,pools[index]);socket_dir=root/('sockets-'+str(index));socket_dir.mkdir(exist_ok=True)
                    adapter=(ConcurrentFailureAdapter if parallel else Adapter)(stores[index],'test',socket_dir)
                    try:
                        with lease.bind():
                            workflows.Runner(stores[index],adapter,threading.Event()).tick(task,step_id=lease.step_id)
                            state=workflows.get(stores[index],task)
                            result=next(x for x in state['steps'] if x['id']==lease.step_id)
                            if result['status']=='succeeded':
                                self.assertEqual((lease.runtime.workspace/result['result'][0]).read_text(),
                                    'join:left:right' if lease.step_id=='join' else lease.step_id)
                    finally:
                        if lease.runtime:lease.runtime.close()
                        lease.release()
                leases=[coordinators[i].claim(task,'workflow',per_user=3,step_id=name) for i,name in enumerate(('left','right'))]
                self.assertTrue(all(leases))
                with ThreadPoolExecutor(max_workers=2) as pool:
                    futures=[pool.submit(execute,i,lease,True) for i,lease in enumerate(leases)]
                    for f in futures:f.result(timeout=60)
                w=workflows.get(stores[0],task)
                self.assertEqual([x['status'] for x in w['steps']],['diagnosing','diagnosing','pending'])
                self.assertEqual(len(stores[0].query('SELECT id FROM workflow_agent_reviews')),1)
                # Fresh coordinator reads durable state, without process-local queues.
                restarted=Coordinator(stores[1],'restarted-reviewer','test',ttl=120)
                for _ in range(2):
                    rows=restarted.candidates();self.assertEqual(rows[0]['workflow_status'],'reviewing')
                    lease=restarted.claim(task,'agent',per_user=3);self.assertIsNotNone(lease)
                    try:
                        with lease.bind():
                            request=workflow_review.begin(stores[1],task);w=workflows.get(stores[1],task)
                            step=next(x for x in w['steps'] if x['id']==request['step'])
                            workflow_review.stage(stores[1],task,{'request_id':request['id'],'action':'repair',
                                'reason':'Remove invalid local argument','step_id':step['id'],
                                'replacement':{**step['spec'],'argv':step['spec']['argv'][:-1]}})
                            workflow_review.commit(stores[1],task,Adapter(stores[1],'test',root/'review-sockets'))
                    finally:lease.release()
                self.assertEqual(len(stores[0].query("SELECT id FROM workflow_agent_reviews WHERE status='applied'")),2)
                for index,name in [(1,'left'),(0,'right'),(1,'join')]:
                    lease=coordinators[index].claim(task,'workflow',per_user=3,step_id=name)
                    self.assertIsNotNone(lease,name);execute(index,lease)
                w=workflows.get(stores[0],task)
                self.assertEqual([x['status'] for x in w['steps']],['succeeded']*3)
                self.assertEqual([x['revision'] for x in w['steps']],[1,1,2])
                self.assertEqual(len(stores[0].query('SELECT * FROM execution_step_checkpoints WHERE workflow=?',(w['id'],))),3)
                self.assertFalse(stores[0].query("SELECT attempt FROM execution_cleanup WHERE state<>'complete'"))
        finally:s3.close();pg.close()

    def test_parallel_user_gate_resumes_existing_review_on_another_node(self):
        from webapp import workflow_review
        pg=PostgresServer();s3=S3Server()
        try:
            with tempfile.TemporaryDirectory(prefix='vu-reviews-') as folder,patch.dict(os.environ,{
                'VIDEO_USE_DATABASE_URL':pg.dsn,'VIDEO_USE_OBJECT_ENDPOINT':s3.endpoint,'VIDEO_USE_OBJECT_BUCKET':'reviews',
                'AWS_ACCESS_KEY_ID':'test','AWS_SECRET_ACCESS_KEY':'test','AWS_DEFAULT_REGION':'us-east-1',
                'VIDEO_USE_DISTRIBUTED':'1','VIDEO_USE_TASK_DISK_BYTES':str(64*1024**2),
                'VIDEO_USE_TASK_MEMORY_BYTES':str(256*1024**2),'VIDEO_USE_NODE_CPU_SLOTS':'8',
            }):
                root=Path(folder);backend=S3Objects('reviews',s3.endpoint);backend.client.create_bucket(Bucket='reviews')
                stores=[Store(root/name) for name in ('a','b')];agent_store.initialize(stores[0])
                project=stores[0].add_project('review-owner','parallel failures')['id']
                plan=agent_store.add_turn(stores[0],project,'plan','two branches and a join')
                agent_store.finish(stores[0],plan,'succeeded','approved')
                task=agent_store.add_turn(stores[0],project,'execute','execute',plan_id=plan)
                coordinators=[Coordinator(store,'reviews-'+str(i),'test',ttl=120) for i,store in enumerate(stores)]
                pools=[IdentityPool(root/('node-'+str(i)),start=750000+i*100,size=4) for i in range(2)]
                seed=coordinators[0].claim(task,'agent');runtime=None
                try:
                    with seed.bind():
                        stores[0].execute("UPDATE agent_turns SET status='running' WHERE id=?",(task,))
                        runtime=TaskRuntime(seed,pools[0]);env=agent_sandbox.prepare(stores[0],project,'test','execute',task)
                        script=env['workspace']/'edit/branch.py'
                        script.write_text("import os,sys,json\nfrom pathlib import Path\n"
                            "if '--fail' in sys.argv:raise ValueError('invalid branch argument')\n"
                            "inputs=json.loads(os.environ.get('VIDEO_USE_STEP_INPUTS','{}'))\n"
                            "text=sys.argv[1]\n"
                            "if text=='join':text+=':'+':'.join(Path(inputs[r][0]).read_text() for r in ('left','right'))\n"
                            "Path(os.environ['VIDEO_USE_STEP_OUTPUT'],'result.txt').write_text(text)\n")
                        os.chown(script,runtime.uid,runtime.gid);runtime.checkpoint()
                finally:
                    if runtime:runtime.close()
                    seed.release()
                def spec(name,deps=()):
                    return {'id':name,'name':name,'kind':'script','depends_on':list(deps),
                        'argv':['python','edit/branch.py',name],
                        'inputs':{dep:{'step':dep,'role':'text'} for dep in deps},
                        'outputs':['result.txt'],'output_roles':{'text':'result.txt'},'retry_safe':False,
                        'goal':'Produce branch output','acceptance':[{'kind':'text','contains':[name]}]+([{'kind':'agent','criterion':'Check the actual branch text'}] if name=='left' else [{'kind':'review','criterion':'Confirm right branch'}] if name=='right' else []),'on_failure':'diagnose'}
                workflows.submit(stores[0],task,{'key':'reviews','name':'parallel reviews','steps':[spec('left'),spec('right'),spec('join',('left','right'))]})
                workflows.state(stores[0],task,'ready');barrier=threading.Barrier(2);left_done=threading.Event()
                class ConcurrentFailureAdapter(Adapter):
                    def local(self,w,step,cancelled):
                        barrier.wait(timeout=20)
                        if step['id']=='right' and not left_done.wait(30):raise TimeoutError('left branch did not queue its review')
                        return super().local(w,step,cancelled)
                def execute(index,lease,parallel=False):
                    lease.runtime_factory=lambda:TaskRuntime(lease,pools[index]);socket_dir=root/('sockets-'+str(index));socket_dir.mkdir(exist_ok=True)
                    adapter=(ConcurrentFailureAdapter if parallel else Adapter)(stores[index],'test',socket_dir)
                    try:
                        with lease.bind():
                            workflows.Runner(stores[index],adapter,threading.Event()).tick(task,step_id=lease.step_id)
                            state=workflows.get(stores[index],task)
                            result=next(x for x in state['steps'] if x['id']==lease.step_id)
                            if result['status']=='succeeded':
                                self.assertEqual((lease.runtime.workspace/result['result'][0]).read_text(),
                                    'join:left:right' if lease.step_id=='join' else lease.step_id)
                    finally:
                        if lease.runtime:lease.runtime.close()
                        lease.release()
                        if parallel and index==0:left_done.set()
                leases=[coordinators[i].claim(task,'workflow',per_user=3,step_id=name) for i,name in enumerate(('left','right'))]
                self.assertTrue(all(leases))
                with ThreadPoolExecutor(max_workers=2) as pool:
                    futures=[pool.submit(execute,i,lease,True) for i,lease in enumerate(leases)]
                    for f in futures:f.result(timeout=60)
                w=workflows.get(stores[0],task)
                self.assertEqual(w['status'],'paused')
                self.assertEqual([x['status'] for x in w['steps']],['agent_review','awaiting_review','pending'])
                request_id=w['agent_review']['id']
                # Exercise the same typed user-decision function used by HTTP.
                from webapp.user_decisions import create,answer
                question=create(stores[0],w,w['steps'][1],{'kind':'quality','question':'Confirm actual right branch?'})
                answer(stores[1],task,question,{'action':'approve','revision':0},'review-owner')
                restarted=Coordinator(stores[1],'restarted-reviewer','test',ttl=120)
                self.assertIsNone(restarted.claim(task,'workflow',step_id='join'))
                self.assertEqual(restarted.candidates()[0]['workflow_status'],'reviewing')
                self.assertEqual(workflows.get(stores[1],task)['agent_review']['id'],request_id)
                lease=restarted.claim(task,'agent',per_user=3);self.assertIsNotNone(lease)
                runtime=None
                try:
                    with lease.bind():
                        runtime=TaskRuntime(lease,pools[1]);request=workflow_review.begin(stores[1],task)
                        w=workflows.get(stores[1],task);left=w['steps'][0]
                        self.assertEqual((runtime.workspace/left['candidate'][0]).read_text(),'left')
                        workflow_review.stage(stores[1],task,{'request_id':request['id'],'action':'pass',
                            'reason':'Read actual restored left branch text',
                            'evidence':[{'step_id':'left','criterion':1,'files':left['candidate'],'observation':'Actual text is left'}]})
                        socket_dir=root/'review-sockets';socket_dir.mkdir()
                        workflow_review.commit(stores[1],task,Adapter(stores[1],'test',socket_dir))
                finally:
                    if runtime:runtime.close()
                    lease.release()
                join=coordinators[0].claim(task,'workflow',per_user=3,step_id='join')
                self.assertIsNotNone(join);execute(0,join)
                w=workflows.get(stores[0],task)
                self.assertEqual([x['status'] for x in w['steps']],['succeeded']*3)
                self.assertEqual([x['attempts'] for x in w['steps']],[1,1,1])
                self.assertEqual([x['revision'] for x in w['steps']],[0,0,0])
                self.assertEqual(len(stores[0].query('SELECT id FROM workflow_agent_reviews')),1)
                self.assertFalse(stores[0].query("SELECT attempt FROM execution_cleanup WHERE state<>'complete'"))
        finally:s3.close();pg.close()

    def test_generated_subtitle_crosses_nodes_into_controlled_media_edit(self):
        import base64
        import numpy as np
        from webapp.execution_intent import declare
        pg=PostgresServer();s3=S3Server()
        try:
            with tempfile.TemporaryDirectory(prefix='vu-subtitle-') as folder,patch.dict(os.environ,{
                'VIDEO_USE_DATABASE_URL':pg.dsn,'VIDEO_USE_OBJECT_ENDPOINT':s3.endpoint,'VIDEO_USE_OBJECT_BUCKET':'subtitles',
                'AWS_ACCESS_KEY_ID':'test','AWS_SECRET_ACCESS_KEY':'test','AWS_DEFAULT_REGION':'us-east-1',
                'VIDEO_USE_DISTRIBUTED':'1','VIDEO_USE_TASK_DISK_BYTES':str(64*1024**2),
                'VIDEO_USE_TASK_MEMORY_BYTES':str(512*1024**2),'VIDEO_USE_TASK_PIDS':'128',
                'VIDEO_USE_OBJECT_ENCRYPTION_KEY':base64.b64encode(b's'*32).decode(),
                'VIDEO_USE_OBJECT_REQUIRE_ENCRYPTION':'1',
            }):
                root=Path(folder);backend=S3Objects('subtitles',s3.endpoint);backend.client.create_bucket(Bucket='subtitles')
                stores=[Store(root/name) for name in ('a','b')]
                agent_store.initialize(stores[0]);project=stores[0].add_project('subtitle-owner','subtitle workflow')['id']
                plan=agent_store.add_turn(stores[0],project,'plan','Generate captions and burn them into video')
                stores[0].execute("UPDATE agent_turns SET status='running' WHERE id=?",(plan,))
                declare(stores[0],plan,{'route':'workflow','operations':['script','media.edit'],
                    'goal':'Generate a timed subtitle and use the registered media editor','estimated_seconds':30})
                agent_store.finish(stores[0],plan,'succeeded','approved')
                task=agent_store.add_turn(stores[0],project,'execute','execute',plan_id=plan)
                coordinators=[Coordinator(store,'subtitle-'+str(i),'test',ttl=120) for i,store in enumerate(stores)]
                pools=[IdentityPool(root/('node-'+str(i)),start=730000+i*100,size=4) for i in range(2)]
                seed=coordinators[0].claim(task,'agent');runtime=None
                try:
                    with seed.bind():
                        stores[0].execute("UPDATE agent_turns SET status='running' WHERE id=?",(task,))
                        runtime=TaskRuntime(seed,pools[0]);env=agent_sandbox.prepare(stores[0],project,'test','execute',task)
                        script=env['workspace']/'edit/make_captions.py'
                        script.write_text("import os,subprocess\nfrom pathlib import Path\n"
                            "out=Path(os.environ['VIDEO_USE_STEP_OUTPUT'])\n"
                            "subprocess.run(['ffmpeg','-v','error','-f','lavfi','-i','color=black:s=320x180:r=10:d=1',"
                            "'-c:v','libx264','-threads','1','-pix_fmt','yuv420p',str(out/'clip.mp4')],check=True)\n"
                            "(out/'captions.srt').write_text('1\\n00:00:00,100 --> 00:00:00,900\\nSubtitle boundary\\n',encoding='utf-8')\n")
                        os.chown(script,runtime.uid,runtime.gid);runtime.checkpoint()
                finally:
                    if runtime:runtime.close()
                    seed.release()
                prepare={'id':'prepare','name':'Create video and captions','kind':'script',
                    'argv':['python','edit/make_captions.py'],'outputs':['clip.mp4','captions.srt'],
                    'output_roles':{'video':'clip.mp4','subtitle':'captions.srt'},'retry_safe':True,
                    'goal':'Produce a black video and valid timed captions','on_failure':'pause',
                    'acceptance':[{'kind':'media','file':'clip.mp4','width':320,'height':180,'video':True},
                                  {'kind':'subtitle','file':'captions.srt'}]}
                render={'id':'burn','name':'Burn generated captions','kind':'operation','operation':'media.edit',
                    'depends_on':['prepare'],'inputs':{'clip':{'step':'prepare','role':'video'},
                                                       'captions':{'step':'prepare','role':'subtitle'}},
                    'parameters':{'edit_plan':{'clips':[{'asset':'clip','start':0,'end':1}],
                        'output':{'width':320,'height':180,'fps':10},
                        'subtitles':{'asset':'captions','size':24,'position':'bottom'}}},
                    'goal':'Render the approved subtitle at its declared timing','on_failure':'pause',
                    'acceptance':[{'kind':'media','width':320,'height':180,'video':True,'decode':True}]}
                workflows.submit(stores[0],task,{'key':'subtitle-flow','name':'Subtitle flow','steps':[prepare,render]})
                workflows.state(stores[0],task,'ready')
                for index,step_id in enumerate(('prepare','burn')):
                    lease=coordinators[index].claim(task,'workflow',step_id=step_id);self.assertIsNotNone(lease)
                    lease.runtime_factory=lambda lease=lease,index=index:TaskRuntime(lease,pools[index])
                    try:
                        with lease.bind():
                            sockets=root/('sockets-'+str(index));sockets.mkdir()
                            runner=workflows.Runner(stores[index],Adapter(stores[index],'test',sockets),threading.Event())
                            runner.tick(task,step_id=step_id)
                            result=next(x for x in workflows.get(stores[index],task)['steps'] if x['id']==step_id)
                            self.assertEqual(result['status'],'succeeded',result['diagnostics'])
                            if step_id=='prepare':
                                subtitle=next(x for x in result['quality']['files'] if x['name'].endswith('.srt'))
                                self.assertEqual(subtitle['kind'],'subtitle')
                            else:
                                self.assertEqual(result['bindings']['captions']['metadata']['kind'],'subtitle')
                                self.assertEqual(result['bindings']['captions']['revision'],0)
                                video=lease.runtime.workspace/next(x for x in result['result'] if x.endswith('.mp4'))
                                def pixels(seconds):
                                    raw=subprocess.check_output(['ffmpeg','-v','error','-i',str(video),'-ss',str(seconds),
                                        '-frames:v','1','-f','rawvideo','-pix_fmt','rgb24','-threads','1','-'])
                                    return np.frombuffer(raw,np.uint8).reshape(180,320,3)
                                self.assertLess(int(pixels(0).max()),10)
                                self.assertGreater(int((pixels(.5)>180).sum()),300)
                    finally:
                        if lease.runtime:lease.runtime.close()
                        lease.release()
                    if index==0:shutil.rmtree(stores[0].root)  # Node B must restore shared, attested artifacts.
        finally:s3.close();pg.close()

    def test_batch_export_preserves_composed_pixels_across_nodes(self):
        import base64
        from PIL import Image,ImageDraw
        from webapp.execution_intent import declare
        pg=PostgresServer();s3=S3Server()
        try:
            with tempfile.TemporaryDirectory(prefix='vu-export-') as folder,patch.dict(os.environ,{
                'VIDEO_USE_DATABASE_URL':pg.dsn,'VIDEO_USE_OBJECT_ENDPOINT':s3.endpoint,
                'VIDEO_USE_OBJECT_BUCKET':'export','AWS_ACCESS_KEY_ID':'test','AWS_SECRET_ACCESS_KEY':'test',
                'AWS_DEFAULT_REGION':'us-east-1','VIDEO_USE_DISTRIBUTED':'1',
                'VIDEO_USE_TASK_DISK_BYTES':str(64*1024**2),'VIDEO_USE_TASK_MEMORY_BYTES':str(512*1024**2),
                'VIDEO_USE_OBJECT_ENCRYPTION_KEY':base64.b64encode(b'e'*32).decode(),
                'VIDEO_USE_OBJECT_REQUIRE_ENCRYPTION':'1',
            }):
                root=Path(folder);backend=S3Objects('export',s3.endpoint);backend.client.create_bucket(Bucket='export')
                stores=[Store(root/name) for name in ('a','b')]
                agent_store.initialize(stores[0]);project=stores[0].add_project('export-owner','export')['id']
                plan=agent_store.add_turn(stores[0],project,'plan','Preserve the composed image when exporting')
                stores[0].execute("UPDATE agent_turns SET status='running' WHERE id=?",(plan,))
                declare(stores[0],plan,{'route':'workflow','operations':['script','image.batch_export'],
                    'goal':'Export without altering layout','estimated_seconds':10})
                agent_store.finish(stores[0],plan,'succeeded','approved')
                task=agent_store.add_turn(stores[0],project,'execute','execute',plan_id=plan)
                coordinators=[Coordinator(store,'export-'+str(i),'test',ttl=120) for i,store in enumerate(stores)]
                pools=[IdentityPool(root/('node-'+str(i)),start=740000+i*100,size=4) for i in range(2)]
                seed=coordinators[0].claim(task,'agent');runtime=None
                try:
                    with seed.bind():
                        stores[0].execute("UPDATE agent_turns SET status='running' WHERE id=?",(task,))
                        runtime=TaskRuntime(seed,pools[0]);env=agent_sandbox.prepare(stores[0],project,'test','execute',task)
                        script=env['workspace']/'edit/compose.py'
                        script.write_text("import os\nfrom pathlib import Path\nfrom PIL import Image,ImageDraw\n"
                            "im=Image.new('RGB',(120,80),'#385939')\n"
                            "ImageDraw.Draw(im).rectangle((0,0,119,4),fill='red')\n"
                            "ImageDraw.Draw(im).rectangle((55,74,65,79),fill='blue')\n"
                            "im.save(Path(os.environ['VIDEO_USE_STEP_OUTPUT'])/'composed.png')\n")
                        os.chown(script,runtime.uid,runtime.gid);runtime.checkpoint()
                finally:
                    if runtime:runtime.close()
                    seed.release()
                prepare={'id':'compose','name':'Compose image','kind':'script','argv':['python','edit/compose.py'],
                    'outputs':['composed.png'],'output_roles':{'image':'composed.png'},'retry_safe':True,
                    'goal':'Produce an image with edge and footer markers','on_failure':'pause',
                    'acceptance':[{'kind':'image','width':120,'height':80}]}
                export={'id':'export','name':'Export composed image','kind':'operation','operation':'image.batch_export',
                    'depends_on':['compose'],'inputs':{'source':{'step':'compose','role':'image'}},
                    'parameters':{'parameters':{'width':120,'height':80,'format':'png','fit':'contain'}},
                    'goal':'Preserve all pixels and marker positions','on_failure':'pause',
                    'acceptance':[{'kind':'image','width':120,'height':80}]}
                workflows.submit(stores[0],task,{'key':'export-flow','name':'Export flow','steps':[prepare,export]})
                workflows.state(stores[0],task,'ready')
                expected=Image.new('RGB',(120,80),'#385939')
                ImageDraw.Draw(expected).rectangle((0,0,119,4),fill='red')
                ImageDraw.Draw(expected).rectangle((55,74,65,79),fill='blue')
                for index,step_id in enumerate(('compose','export')):
                    lease=coordinators[index].claim(task,'workflow',step_id=step_id);self.assertIsNotNone(lease)
                    lease.runtime_factory=lambda lease=lease,index=index:TaskRuntime(lease,pools[index])
                    try:
                        with lease.bind():
                            sockets=root/('sockets-'+str(index));sockets.mkdir()
                            workflows.Runner(stores[index],Adapter(stores[index],'test',sockets),threading.Event()).tick(task,step_id=step_id)
                            result=next(x for x in workflows.get(stores[index],task)['steps'] if x['id']==step_id)
                            self.assertEqual(result['status'],'succeeded',result['diagnostics'])
                            image=lease.runtime.workspace/next(x for x in result['result'] if x.endswith('.png'))
                            with Image.open(image) as actual:self.assertEqual(actual.convert('RGB').tobytes(),expected.tobytes())
                            if index:self.assertEqual(result['bindings']['source']['revision'],0)
                    finally:
                        if lease.runtime:lease.runtime.close()
                        lease.release()
                    if index==0:shutil.rmtree(stores[0].root)
        finally:s3.close();pg.close()

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
