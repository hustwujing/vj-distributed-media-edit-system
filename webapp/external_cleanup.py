"""Bounded cancellation/confirmation only; never submits a generation job."""
import json
import re
import signal
import sys
import uuid
import os
from pathlib import Path
from .store import Store
from .egress_policy import session,provider_base


def run(store,identifier):
    from .operation_extensions import load
    load()
    owner=uuid.uuid4().hex
    with store.connect() as db:
        db.execute('BEGIN IMMEDIATE')
        row=db.execute('SELECT * FROM execution_external_cleanup WHERE id=?',(identifier,)).fetchone()
        now=store.now()
        if not row or row['state'] in ('complete','unknown','unconfirmed','unsupported') or row['lease_until']>now or row['next_retry']>now:return
        row=dict(row)
        if row['attempts']>=3 or row['deadline_at'] and now>=row['deadline_at']:
            db.execute("UPDATE execution_external_cleanup SET state='unconfirmed',error='未确认上游取消，上游可能继续运行。' WHERE id=?",(identifier,));return
        deadline=row['deadline_at'] or now+max(20,float(os.environ.get('VIDEO_USE_EXTERNAL_CLEANUP_SECONDS','300')))
        db.execute("UPDATE execution_external_cleanup SET state='running',attempts=attempts+1,generation=generation+1,owner=?,deadline_at=?,lease_until=?,phase='poll' WHERE id=?",(owner,deadline,min(now+20,deadline),identifier))
    state,error,next_phase='pending','', 'poll'
    try:
        if not re.fullmatch(r'[A-Za-z0-9_-]{1,100}',row['external_id']):raise ValueError('Invalid upstream ID')
        base=provider_base(row['provider'])
        method='POST' if row['phase']=='cancel' else 'GET'
        url=base+'/api/jobs/'+row['external_id']+('/cancel' if method=='POST' else '')
        calls=store.query('SELECT implementation FROM workflow_provider_calls WHERE request_key=?',(identifier,))
        handle=json.loads(calls[0]['implementation']) if calls else {}
        from . import generation_service
        if calls:
            from .provider_metadata import route
            policy=route(handle['route_adapter']).cleanup_category
            if policy!=handle['cleanup_category']:raise ValueError('Invalid saved cleanup policy')
        else:
            direct=store.query('SELECT id FROM execution_provider_calls WHERE id=?',(identifier,))
            if not direct:raise ValueError('Cleanup has no frozen provider call')
            policy='job_api'
        if policy=='generation_protocol':
            with session() as client:
                result=(generation_service.cancel(client,handle,row['external_id']) if method=='POST'
                        else generation_service.poll(client,handle,row['external_id']))
            if result.get('status') in ('cancelled','failed','succeeded'):
                state='complete';row['_provider_terminal']=result['status']
                row['_usage']=result.get('usage');row['_cost']=result.get('cost')
            elif method=='GET':next_phase='cancel'
        elif policy=='unqueryable_sync':
            raise NotImplementedError('Synchronous provider has no queryable cancellation handle')
        elif policy=='job_api':
            state,error,next_phase=job_api_cleanup(store,row,base,method,url,state,error,next_phase)
    except NotImplementedError:
        state='unsupported';error='该上游接口不支持已验证的取消能力，任务可能继续运行。'
    except Exception as exc:
        from .telemetry import emit
        emit('provider.cleanup_failed','WARNING',exc=exc,task_id=row['task_id'],external_id=row['external_id'])
        error='尚未确认上游停止，将在收尾预算内继续核实。'
    with store.connect() as db:
        db.execute('BEGIN IMMEDIATE')
        if row['attempts']+1>=3 and state not in ('complete','unsupported'):state='unconfirmed';error='未确认上游取消，上游可能继续运行。'
        changed=db.execute('UPDATE execution_external_cleanup SET state=?,phase=?,lease_until=0,next_retry=?,error=? WHERE id=? AND generation=? AND owner=? AND lease_until>?',
                   (state,next_phase,store.now()+5*(row['attempts']+1),error,identifier,row['generation']+1,owner,store.now())).rowcount
        if changed and state=='complete':
            terminal=row.get('_provider_terminal')
            if terminal:
                db.execute("UPDATE workflow_provider_calls SET state=?,usage=CASE WHEN ?<>'null' THEN ? ELSE usage END,cost=CASE WHEN ?<>'null' THEN ? ELSE cost END,updated=? WHERE request_key=? AND state NOT IN ('succeeded','failed','cancelled')",
                    (terminal,json.dumps(row.get('_usage')),json.dumps(row.get('_usage')),json.dumps(row.get('_cost')),json.dumps(row.get('_cost')),store.now(),identifier))
        parts=identifier.split(':')
        if changed and state=='complete' and len(parts)==3 and parts[2].isdigit():
            db.execute("UPDATE agent_steps SET error='上游任务已确认结束。' WHERE workflow=? AND id=? AND revision=? AND status='cancelled' AND external_id=?",
                       (parts[0],parts[1],int(parts[2]),row['external_id']))


def job_api_cleanup(store,row,base,method,url,state,error,next_phase):
    with session() as client:
        saved=store.query('SELECT cookies FROM workflow_provider_calls WHERE provider=? AND external_id=? ORDER BY created DESC LIMIT 1',(base,row['external_id']))
        if saved:client.cookies.update(json.loads(saved[0]['cookies']))
        calls=store.query('SELECT implementation FROM workflow_provider_calls WHERE request_key=?',(row['id'],))
        if calls and json.loads(calls[0]['implementation'])['route_adapter']=='generation_jobs':
            from .generation_provider import endpoint
            if base!=endpoint():raise ValueError('Restore the original generation credential route')
            from .generation_provider import headers
            client.headers.update(headers())
        with client.request(method,url,timeout=(3,5),allow_redirects=False,stream=True) as response:
            if method=='POST' and response.status_code in (405,501):
                state='unsupported';error='上游不支持取消，任务可能继续运行。'
            else:response.raise_for_status()
            data=bytearray()
            for chunk in response.iter_content(4096):
                data.extend(chunk)
                if len(data)>65536:raise ValueError('Oversized status response')
            result=json.loads(data) if data and state!='unsupported' else {}
    if result.get('status') in ('cancelled','canceled','failed','succeeded','completed'):
        state='complete'
        row['_provider_terminal']={'canceled':'cancelled','completed':'succeeded'}.get(result['status'],result['status'])
        row['_usage']=result.get('usage');row['_cost']=result.get('cost')
    elif method=='GET':next_phase='cancel'
    return state,error,next_phase


def reconcile(store,identifier):
    """Operator-requested bounded status lookup, never a new generation request."""
    with store.connect() as db:
        db.execute('BEGIN IMMEDIATE')
        row=db.execute('SELECT * FROM execution_external_cleanup WHERE id=?',(identifier,)).fetchone()
        if not row or not row['external_id']:raise ValueError('核实需要已知上游任务 ID')
        if row['state'] not in ('unknown','unconfirmed','unsupported'):raise ValueError('清理仍在执行或已确认结束')
        db.execute("UPDATE execution_external_cleanup SET state='pending',phase='poll',attempts=0,next_retry=0,lease_until=0,deadline_at=0,reconciliations=reconciliations+1 WHERE id=?",(identifier,))

if __name__=='__main__':
    signal.signal(signal.SIGALRM,signal.SIG_DFL);signal.alarm(12)
    from .telemetry import setup,bind,span
    setup('external_cleanup')
    store=Store(Path(sys.argv[1]))
    records=store.query('SELECT task_id FROM execution_external_cleanup WHERE id=?',(sys.argv[2],))
    with bind(task_id=records[0]['task_id'] if records else None),span('external_cleanup'):
        run(store,sys.argv[2])
