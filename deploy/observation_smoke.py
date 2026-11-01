"""Explicit live acceptance using user HTTP routes and private test projects.

Includes brief observer and business maintenance; requires an idle cluster.
No model generation, no message notifications, no business-state seeding.
"""
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import time

REPO=Path(__file__).resolve().parents[1];sys.path.insert(0,str(REPO))
from deploy import cluster


def wait(function,label,seconds=90):
    end=time.monotonic()+seconds
    while time.monotonic()<end:
        result=function()
        if result:return result
        time.sleep(.3)
    raise RuntimeError('Timed out: '+label)


def main():
    release=json.loads((cluster.ROOT/'release.json').read_text());sys.path.insert(0,release['source'])
    os.environ.update(cluster.environment());os.environ.update(cluster.env_file(cluster.CONF/'observation.env'))
    import requests
    from PIL import Image
    from webapp.store import Store
    from webapp.diagnostics import diagnose,observation
    store=Store(cluster.data('node','a'),initialize_schema=False,timeout=1)
    client=requests.Session();client.trust_env=False
    base='http://127.0.0.1:18765';report={'passed':False,'release':release,'scope':'user HTTP submission, real worker, real BOSS, live journal collector'}
    business_stopped=False;observer_stopped=False
    try:
        r=client.post(base+'/api/projects',json={'name':'可观测性现场验收 '+time.strftime('%H:%M:%S')},timeout=10);r.raise_for_status()
        project=r.json()['id'];client.headers['Cookie']='video_use_session='+r.cookies['video_use_session'];report['project']=project
        image=io.BytesIO();Image.new('RGB',(128,96),'blue').save(image,'PNG')
        r=client.post(base+f'/api/projects/{project}/assets',files={'file':('observability.png',image.getvalue(),'image/png')},timeout=30);r.raise_for_status()
        asset=r.json()['id'];upload_request=r.headers['X-Request-ID'];report['upload_request']=upload_request
        r=client.post(base+f'/api/projects/{project}/image-jobs',json={'action':'local_adjust','asset':asset,'parameters':{'brightness':.7}},timeout=15);r.raise_for_status()
        task=r.json()['id'];report['task']=task;report['submission_request']=r.headers['X-Request-ID']
        def finished():
            value=store.job(task)
            if value['status'] in ('failed','cancelled'):raise RuntimeError('Live manual operation stopped: '+value['error'])
            return value if value['status']=='succeeded' else None
        wait(finished,'local adjustment output',120)
        r=client.get(base+f'/api/projects/{project}',timeout=10);r.raise_for_status()
        job=next(j for j in r.json()['jobs'] if j['id']==task)
        artifact=next(a for a in job['artifacts'] if a['name'].endswith('.png'))
        r=client.get(base+'/'+artifact['url'].lstrip('/'),timeout=15);r.raise_for_status()
        with Image.open(io.BytesIO(r.content)) as image:
            assert image.size==(128,96) and 140<image.convert('RGB').getpixel((0,0))[2]<255
        report['real_output_valid']=True
        link=store.query('SELECT request_id FROM observation_tasks WHERE task_id=?',(task,))[0]
        assert link['request_id']==report['submission_request'];report['durable_request_link']=True
        wait(lambda:observation('/logs',{'task_id':task,'limit':100})['events'],'task journal ingestion',20)
        media=wait(lambda:[e for e in observation('/logs',{'request_id':upload_request,'limit':100})['events'] if e.get('phase')=='media_decode'],'API to decoder correlation',20)
        report['media_request_correlated']=bool(media)
        root_report=diagnose(store,task);assert root_report['attempts'] and root_report['timeline'] and root_report['logs']
        report['diagnostic_gaps']=root_report['gaps'];report['task_diagnosis']=True
        r=client.get('http://127.0.0.1:18767'+f'/api/projects/{project}/diagnostics/{task}',timeout=15);r.raise_for_status()
        assert all('node' not in a for a in r.json()['attempts']);report['cross_api_owner_diagnosis']=True
        stranger=requests.Session();stranger.trust_env=False
        assert stranger.get(base+f'/api/projects/{project}/diagnostics/{task}',timeout=10).status_code==404
        assert stranger.get('http://127.0.0.1:18768/logs',timeout=5).status_code==401
        report['authorization_verified']=True
        # Only independent test projects have been created; do not stop real work.
        busy=store.query("SELECT id FROM agent_turns WHERE status IN ('queued','running','waiting') UNION ALL SELECT id FROM jobs WHERE status IN ('queued','running')")
        if busy:raise RuntimeError('Live business tasks present; do not start lifecycle maintenance')
        cluster.run(['systemctl','stop',cluster.OBS_SERVICE]);observer_stopped=True
        started=time.monotonic();r=client.get(base+'/api/health',timeout=5);r.raise_for_status();outage_request=r.headers['X-Request-ID']
        report['collector_outage_api_seconds']=round(time.monotonic()-started,3)
        cluster.run(['systemctl','start',cluster.OBS_SERVICE]);observer_stopped=False
        def replayed():
            try:return observation('/logs',{'request_id':outage_request})['events']
            except Exception:return None
        wait(replayed,'journal replay after collector outage',30);report['collector_outage_recovered']=True
        subprocess.run([str(REPO/'deploy.sh'),'stop'],cwd=REPO,check=True,timeout=180);business_stopped=True
        assert cluster.active(cluster.OBS_SERVICE)
        partial=diagnose(store,task);assert not partial['complete'] and partial['logs']
        report['database_outage_partial_evidence']=True
        subprocess.run([str(REPO/'deploy.sh'),'start'],cwd=REPO,check=True,timeout=180);business_stopped=False
        wait(lambda:cluster.active(cluster.OBS_SERVICE),'observer remains active',10)
        def healthy():
            try:
                h=observation('/health')
                return h if h['ready'] and h['business_dependencies'].get('database')=='available' and h['business_dependencies'].get('object_storage')=='available' else None
            except Exception:return None
        report['health']=wait(healthy,'post-maintenance dependencies',30)
        report['services']={name:cluster.active(name) for name in [cluster.DB_SERVICE,*cluster.APIS,*cluster.WORKERS,*cluster.REAPERS,*cluster.MEDIA,cluster.OBS_SERVICE]}
        assert all(report['services'].values())
        output=REPO/'docs/test-evidence/observability-task-diagnosis.json'
        output.write_text(json.dumps(diagnose(store,task),ensure_ascii=False,indent=2)+'\n');output.chmod(0o600)
        report['passed']=True
    finally:
        if observer_stopped:cluster.run(['systemctl','start',cluster.OBS_SERVICE])
        if business_stopped:subprocess.run([str(REPO/'deploy.sh'),'start'],cwd=REPO,check=True,timeout=180)
        (REPO/'docs/test-evidence/observability-live.json').write_text(json.dumps(report,ensure_ascii=False,indent=2)+'\n')
    print(json.dumps(report,ensure_ascii=False))


if __name__=='__main__':main()
