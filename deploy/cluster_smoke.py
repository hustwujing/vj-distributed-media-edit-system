"""Explicit deployed-cluster acceptance, using only this script's own test tasks."""
import io
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import time
import uuid

REPO=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(REPO))
from deploy import cluster


def wait(predicate,label,timeout=180):
    deadline=time.monotonic()+timeout
    while time.monotonic()<deadline:
        result=predicate()
        if result:return result
        time.sleep(.5)
    raise RuntimeError('Timed out: '+label)


def service(action,name):cluster.run(['systemctl',action,name],timeout=120)


def seed(store,project,mode):
    from webapp import agent_store,agent_sandbox,workflows,execution_intent
    from webapp.task_leases import Coordinator
    from webapp.execution_resources import IdentityPool
    from webapp.execution_runtime import TaskRuntime
    pool=IdentityPool(cluster.registry('a'),start=cluster.UIDS['a'],size=64)
    coordinator=Coordinator(store,pool.node,'deployment-smoke')
    with store.connect() as db:
        db.execute('BEGIN IMMEDIATE')
        plan=agent_store.add_turn(store,project,'plan','部署验收：本地图片处理，不调用图片或视频生成服务。')
        store.execute("UPDATE agent_turns SET status='running' WHERE id=?",(plan,))
        execution_intent.declare(store,plan,{'route':'workflow','operations':['script'],'goal':'验证本地分支与跨节点接管','estimated_seconds':120})
        text=('确认：用本地Pillow生成两张16×16 RGB图片，左图纯蓝(0,0,255)，右图纯绿(0,128,0)，再水平拼接为32×16，最终交付image.png。实际查看并检验像素即可，不需要外部生成。' if mode=='branches' else '确认部署取消测试：等待60秒后生成16×16纯蓝PNG，期间允许取消，不调用外部服务。')
        agent_store.finish(store,plan,'succeeded',text)
        turn=agent_store.add_turn(store,project,'execute','执行已确认的部署自检',plan_id=plan)
        store.execute("UPDATE agent_turns SET status='running' WHERE id=?",(turn,))
        lease=coordinator.claim(turn,'agent')
        if not lease:raise RuntimeError('Could not reserve smoke preparation')
    runtime=None
    try:
        with lease.bind():
            # This fixture bypasses GatewayProcess.start(), which normally
            # creates the node-local project layout before saving checkpoints.
            for relative in ('sources','edit/transcripts'):
                (store.project_dir(project)/relative).mkdir(mode=0o700,parents=True,exist_ok=True)
            runtime=TaskRuntime(lease,pool);env=agent_sandbox.prepare(store,project,'test','execute',turn)
            script=env['workspace']/'edit/deployment_test.py'
            script.write_text('''import json,os,sys,time
from pathlib import Path
from PIL import Image
out=Path(os.environ['VIDEO_USE_STEP_OUTPUT']);kind=sys.argv[1]
(out/'started.txt').write_text('started')
if kind=='join':
    inputs=json.loads(os.environ['VIDEO_USE_STEP_INPUTS'])
    result=Image.new('RGB',(32,16))
    for x,role in ((0,'left'),(16,'right')):
        with Image.open(inputs[role][0]) as im:result.paste(im.convert('RGB'),(x,0))
else:
    time.sleep(60 if kind=='cancel' else 8)
    result=Image.new('RGB',(16,16),'green' if kind=='right' else 'blue')
result.save(out/'image.png')
''');os.chown(script,runtime.uid,runtime.gid);script.chmod(0o644)
            runtime.checkpoint()
            def step(name,deps=()):
                return {'id':name,'name':name,'kind':'script','argv':['python','edit/deployment_test.py',name],
                    'inputs':{d:{'step':d,'role':'primary_image'} for d in deps},'depends_on':list(deps),
                    'outputs':['image.png'],'output_roles':{'primary_image':'image.png'},'retry_safe':True,
                    'goal':'本地部署自检 '+name,'on_failure':'pause',
                    'acceptance':[{'kind':'image','width':32 if name=='join' else 16,'height':16}]}
            steps=[step('left'),step('right'),step('join',('left','right'))] if mode=='branches' else [step('cancel')]
            workflows.submit(store,turn,{'key':'deployment-'+uuid.uuid4().hex,'name':'部署自检 '+mode,'steps':steps})
            workflows.state(store,turn,'ready')
    finally:
        if runtime:runtime.close()
        lease.release()
        coordinator.heartbeat(draining=True)
    return turn


def main():
    release=json.loads((cluster.ROOT/'release.json').read_text())
    # Import the exact deployed application, even while the shared workspace changes.
    sys.path.insert(0,release['source'])
    import requests
    from PIL import Image
    from webapp.store import Store
    from webapp import agent_store,workflows
    os.environ.update(cluster.environment('a'))
    store=Store(cluster.data('node','a'))
    client=requests.Session();client.trust_env=False
    base='http://127.0.0.1:18765'
    report={'passed':False,'release':release,'scope':'Deployed API replicas, independent worker systemd units, real PostgreSQL and encrypted BOSS; one physical host'}
    try:
        response=client.post(base+'/api/projects',json={'name':'集群验收 '+time.strftime('%H:%M:%S')},timeout=10);response.raise_for_status();project=response.json()['id']
        # Direct loopback probes omit /video-use while the browser cookie is
        # correctly scoped to that external prefix. Carry the same test identity.
        client.headers['Cookie']='video_use_session='+response.cookies['video_use_session']
        report['project']=project
        image=io.BytesIO();Image.new('RGB',(16,16),'blue').save(image,'PNG')
        response=client.post('http://127.0.0.1:18766/api/projects/'+project+'/assets',files={'file':('probe.png',image.getvalue(),'image/png')},timeout=30)
        response.raise_for_status();asset=response.json()['id']
        shutil.rmtree(cluster.data('api','a')/'projects'/project)
        result=client.get(f'http://127.0.0.1:18767/api/projects/{project}/assets/{asset}/file',timeout=30)
        result.raise_for_status();assert result.content==image.getvalue()
        report['api_cross_node_file']=True
        service('stop',cluster.APIS[0])
        response=client.get(base+f'/api/projects/{project}',timeout=10);response.raise_for_status()
        assert '18767' in response.headers.get('X-Video-Use-API','')
        report['api_failover']=True;service('start',cluster.APIS[0])
        service('stop',cluster.WORKERS[1])
        task=seed(store,project,'branches');report['workflow_task']=task
        def running_left():
            rows=store.query("SELECT a.id,a.runtime,w.node FROM execution_attempts a JOIN execution_workers w ON w.id=a.worker WHERE a.run_id=? AND a.step_id='left' AND a.status='running'",(task,))
            if rows and rows[0]['runtime']:
                pid=cluster.run(['systemctl','show',cluster.WORKERS[0],'--property=MainPID','--value']).stdout.strip()
                marker=cluster.run(['nsenter','-t',pid,'-m','--','find',str(Path(rows[0]['runtime'])/'data/workspace/edit/workflows'),'-name','started.txt','-print','-quit'],check=False)
                if marker.returncode==0 and marker.stdout.strip():return rows[0]
        first=wait(running_left,'node A running first branch',90)
        report['failed_attempt']=first['id'];report['original_node']=first['node']
        service('start',cluster.WORKERS[1])
        cluster.run(['systemctl','kill','--signal=SIGKILL','--kill-whom=all',cluster.WORKERS[0]])
        service('stop',cluster.WORKERS[0])
        # Delete only this test project's node-A cache. Its committed files are in BOSS.
        shutil.rmtree(store.project_dir(project),ignore_errors=True)
        print('Node A killed; waiting for B to recover and finish branches.',flush=True)
        def finished():
            row=agent_store.turn(store,task)
            if row['status'] in ('failed','paused','cancelled'):raise RuntimeError('Cross-node workflow stopped: '+row['error'])
            return row if row['status']=='succeeded' else None
        row=wait(finished,'node B workflow and final Agent review',240)
        workflow=workflows.get(store,task)
        assert all(s['status']=='succeeded' for s in workflow['steps'])
        assert workflow['steps'][0]['attempts']>=2
        attempts=store.query('SELECT a.step_id,a.status,w.node FROM execution_attempts a JOIN execution_workers w ON w.id=a.worker WHERE a.run_id=?',(task,))
        assert any(a['step_id']=='left' and a['status']=='finished' and a['node']!=first['node'] for a in attempts)
        report['cross_node_recovery']=True;report['attempts']=attempts
        final=next(a for a in row['artifacts'] if a.get('workflow_delivery'))
        response=client.get(base+'/'+final['url'].lstrip('/'),timeout=30);response.raise_for_status()
        with Image.open(io.BytesIO(response.content)) as result:
            assert result.size==(32,16) and result.convert('RGB').getpixel((0,0))==(0,0,255) and result.convert('RGB').getpixel((31,0))==(0,128,0)
        report['join_and_delivery']=True
        service('start',cluster.WORKERS[0])
        response=client.post(base+'/api/projects',json={'name':'集群取消验收 '+time.strftime('%H:%M:%S')},timeout=10);response.raise_for_status();cancel_project=response.json()['id']
        cancel_task=seed(store,cancel_project,'cancel');report['cancel_task']=cancel_task
        wait(lambda:store.query("SELECT id FROM execution_attempts WHERE run_id=? AND step_id='cancel' AND runtime<>'' AND status='running'",(cancel_task,)),'cancel fixture executing',90)
        started=time.monotonic();response=client.post(f'http://127.0.0.1:18767/api/projects/{cancel_project}/agent/{cancel_task}/cancel',timeout=10);response.raise_for_status()
        assert response.json()['cancelled']
        wait(lambda:not store.query('SELECT attempt FROM execution_leases WHERE run_id=? AND active=1',(cancel_task,)),'cancelled execution release',30)
        wait(lambda:not store.query("SELECT attempt FROM execution_cleanup WHERE task_id=? AND state<>'complete'",(cancel_task,)),'cancel cleanup',30)
        report['cancel_seconds']=round(time.monotonic()-started,3);report['cross_api_cancel']=True
        assert agent_store.turn(store,cancel_task)['status']=='cancelled'
        assert not any(a.get('workflow_delivery') for a in agent_store.turn(store,cancel_task)['artifacts'])
        wait(lambda:not store.query("SELECT attempt FROM execution_cleanup WHERE task_id=? AND state<>'complete'",(task,)),'old node resource cleanup',60)
        report['passed']=True
    finally:
        for name in cluster.APIS+cluster.REAPERS+cluster.WORKERS:
            if not cluster.active(name):service('start',name)
        Path('docs/test-evidence/cluster-deployment-smoke.json').write_text(json.dumps(report,ensure_ascii=False,indent=2)+'\n')
    print(json.dumps(report,ensure_ascii=False))


if __name__=='__main__':main()
