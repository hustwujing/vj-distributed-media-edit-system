"""Deterministic live recovery checks for the dedicated OpenSandbox deployment."""
from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import time
import uuid
from urllib.parse import urlsplit

from webapp.sandbox_backends import BackendBinding,OPEN_SANDBOX,OpenSandboxClient,RequestsTransport
from webapp.store import Store


ROOT=Path(os.environ.get('DYNAMIC_CASE_DEPLOY_ROOT','')).resolve()
UNIT='sandbox-agent-opensandbox.service'


def require_scope():
    if ROOT.name!='.sandbox-cluster' or ROOT.parent.name!='distributed-agent-sandbox-backend':
        raise RuntimeError('Recovery test requires the dedicated sandbox deployment root')
    dsn=urlsplit(os.environ.get('VIDEO_USE_DATABASE_URL',''))
    if dsn.path!='/distributed_agent_sandbox':raise RuntimeError('Recovery test refused a non-sandbox database')
    if os.environ.get('VIDEO_USE_DEPLOYMENT')!='sandbox-agent':raise RuntimeError('Recovery test refused another deployment')
    if os.geteuid()!=0:raise RuntimeError('Recovery test requires root for bounded service faults')


def service(action):
    subprocess.run(['systemctl',action,UNIT],check=True,timeout=60)


def wait_health(transport,seconds=45):
    deadline=time.monotonic()+seconds
    while time.monotonic()<deadline:
        try:
            status,_=transport.request('GET','/health')
            if status==200:return
        except Exception:pass
        time.sleep(.5)
    raise TimeoutError('OpenSandbox service did not recover')


def wait_row(store,key,predicate,seconds=60):
    deadline=time.monotonic()+seconds;row=None
    while time.monotonic()<deadline:
        rows=store.query('SELECT state,phase,attempts,error FROM execution_external_cleanup WHERE id=?',(key,))
        row=rows[0] if rows else None
        if row and predicate(row):return row
        time.sleep(.5)
    raise TimeoutError('Cleanup row did not reach the expected state: '+repr(row))


def create(client,binding,attempt):
    lease=type('Lease',(),{'attempt':attempt,'generation':1,'run_id':uuid.uuid4().hex})()
    value=client.create(lease,binding,{'cpu':1,'memory_mb':512},timeout=120,entrypoint=['tail','-f','/dev/null'])
    if not value.get('id'):raise RuntimeError('OpenSandbox create returned no id')
    return value['id']


def main():
    require_scope();ROOT.joinpath('recovery').mkdir(parents=True,exist_ok=True,mode=0o700)
    store=Store(ROOT/'node-a')
    active=store.query("SELECT id FROM runs WHERE status NOT IN ('SUCCEEDED','FAILED','CANCELLED') LIMIT 1")
    if active:raise RuntimeError('Recovery faults require the dedicated workflow cluster to be quiescent')
    base=os.environ['VIDEO_USE_OPEN_SANDBOX_URL'].rstrip('/');key=os.environ['VIDEO_USE_OPEN_SANDBOX_API_KEY']
    transport=RequestsTransport(base,key,timeout=5)
    client=OpenSandboxClient(transport,network_policy=os.environ.get('VIDEO_USE_OPEN_SANDBOX_NETWORK_ISOLATION','deny'))
    binding=BackendBinding(OPEN_SANDBOX,os.environ['VIDEO_USE_OPEN_SANDBOX_COMMAND_RUNTIME_PROFILE'],
        os.environ['VIDEO_USE_OPEN_SANDBOX_COMMAND_IMAGE_DIGEST'],
        os.environ['VIDEO_USE_OPEN_SANDBOX_COMMAND_BUNDLE_DIGEST'],'command_runtime')
    report={'started_at':time.time(),'release':json.loads((ROOT/'release.json').read_text()),'cases':[]}
    rows=[];owned={}
    try:
        wait_health(transport)
        for name,known_id in (('known_id_orphan',True),('unknown_create_receipt',False)):
            attempt=uuid.uuid4().hex;cleanup='opensandbox:'+attempt;sandbox=create(client,binding,attempt);owned[attempt]=sandbox
            store.execute("""INSERT INTO execution_external_cleanup
                (id,task_id,provider,external_id,state,phase,error) VALUES (?,?,?,?,?,'delete','')""",
                (cleanup,uuid.uuid4().hex,cleanup,sandbox if known_id else '','dormant'))
            rows.append(cleanup)
            final=wait_row(store,cleanup,lambda row:row['state']=='complete')
            status,_=transport.request('GET','/v1/sandboxes/'+sandbox)
            if status!=404:raise RuntimeError(name+' left a sandbox behind')
            owned.pop(attempt,None);report['cases'].append({'name':name,'passed':True,'attempt':attempt,
                'sandbox_id':sandbox,'cleanup':final,'confirmed_http_status':status})

        attempt=uuid.uuid4().hex;cleanup='opensandbox:'+attempt;sandbox=create(client,binding,attempt);owned[attempt]=sandbox
        store.execute("""INSERT INTO execution_external_cleanup
            (id,task_id,provider,external_id,state,phase,error) VALUES (?,?,?,?,?,'delete','')""",
            (cleanup,uuid.uuid4().hex,cleanup,sandbox,'pending'))
        rows.append(cleanup);service('stop')
        failed=wait_row(store,cleanup,lambda row:row['attempts']>=1 and row['state'] in ('pending','running'))
        service('start');wait_health(transport);store.execute('UPDATE execution_external_cleanup SET next_retry=0,lease_until=0 WHERE id=?',(cleanup,))
        final=wait_row(store,cleanup,lambda row:row['state']=='complete')
        status,_=transport.request('GET','/v1/sandboxes/'+sandbox)
        if status!=404:raise RuntimeError('Service recovery left a sandbox behind')
        owned.pop(attempt,None);report['cases'].append({'name':'cleanup_service_outage','passed':True,'attempt':attempt,
            'sandbox_id':sandbox,'failure_observed':failed,'cleanup':final,'confirmed_http_status':status})
        report.update(passed=True,finished_at=time.time())
    finally:
        try:service('start');wait_health(transport)
        except Exception as exc:report['service_restore_error']=type(exc).__name__
        for attempt,sandbox in list(owned.items()):
            try:client.delete_confirmed(sandbox,expected_attempt=attempt)
            except Exception as exc:(report.setdefault('residual_cleanup_errors',[])).append(type(exc).__name__)
        if rows:store.execute('DELETE FROM execution_external_cleanup WHERE id IN ('+','.join('?' for _ in rows)+')',tuple(rows))
        transport.close()
        target=ROOT/'recovery/opensandbox-recovery.json';target.write_text(json.dumps(report,indent=2,sort_keys=True)+'\n');target.chmod(0o600)
    print(json.dumps({'passed':report.get('passed',False),'cases':len(report['cases']),'report':str(target)}))
    return 0 if report.get('passed') else 1


if __name__=='__main__':raise SystemExit(main())
