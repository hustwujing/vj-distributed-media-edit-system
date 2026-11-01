"""Durable phases for standalone asynchronous image jobs, without nested tasks."""
import json
import os
from pathlib import Path
import shutil
import stat
from . import agent_sandbox
from .workflows import Step


def initialize(db):
    db.executescript('''CREATE TABLE IF NOT EXISTS manual_operation_runs(
      job_id TEXT PRIMARY KEY,state TEXT NOT NULL,spec TEXT NOT NULL,external_id TEXT,
      bindings TEXT NOT NULL DEFAULT '{}',next_poll REAL NOT NULL DEFAULT 0,failures INTEGER NOT NULL DEFAULT 0,
      created REAL NOT NULL,updated REAL NOT NULL
    );''')


def supported(job):
    return job['kind']=='image' and job['payload'].get('image_task',{}).get('action') in ('generate','decompose','erase','local_edit')


def tick(host,lease):
    store=host.store;job=store.job(lease.turn_id)
    if not supported(job):return False
    if job['status'] in ('cancelled','failed','succeeded'):return True
    rows=store.query('SELECT * FROM manual_operation_runs WHERE job_id=?',(job['id'],))
    if not rows:
        task=job['payload']['image_task']
        from .operations import for_image,IMAGE_PARAMETERS
        params={k:v for k,v in task.items() if k in IMAGE_PARAMETERS[task['action']]}
        inputs={'source':{'asset':task['asset']}}
        if task.get('mask'):inputs['mask']={'asset':task['mask']}
        for i,ref in enumerate(task.get('references',[])):inputs['reference' if i==0 else 'reference2']={'asset':ref}
        spec=Step(id='image',name='图片处理',kind='operation',operation=for_image(task['action']).id,parameters=params,inputs=inputs).model_dump()
        store.execute('INSERT INTO manual_operation_runs(job_id,state,spec,created,updated) VALUES (?,?,?,?,?)',
            (job['id'],'pending',json.dumps(spec),store.now(),store.now()))
        rows=store.query('SELECT * FROM manual_operation_runs WHERE job_id=?',(job['id'],))
    row=rows[0]
    if row['next_poll']>store.now():return True
    if row['state']=='submitting':
        store.finish(job['id'],'failed',error='外部提交结果未知，未自动重复提交。');return True
    w={'id':job['id'],'turn_id':job['id'],'project':job['project'],'manual':True,'steps':[]}
    s={'id':'image','spec':json.loads(row['spec']),'revision':0,'attempts':0,'external_id':row['external_id'],'bindings':json.loads(row['bindings'])}
    adapter=host.workflow_runner.adapter
    phase='poll' if row['external_id'] else 'submit'
    try:
        if phase=='submit':
            store.execute("UPDATE manual_operation_runs SET state='submitting',updated=? WHERE job_id=?",(store.now(),job['id']))
            identifier=adapter.submit(w,s)
            store.execute("UPDATE manual_operation_runs SET state='waiting',external_id=?,next_poll=?,updated=? WHERE job_id=?",(identifier,store.now()+2,store.now(),job['id']))
            store.execute("UPDATE jobs SET status='queued' WHERE id=? AND status<>'cancelled'",(job['id'],))
            return True
        record=adapter.poll(w,s)
        if record['status'] in ('failed','cancelled'):
            from .generation_errors import task_failure
            from .telemetry import emit
            failure=task_failure(record)
            emit('provider.task_failed','ERROR',exc=failure,external_id=s['external_id'])
            with store.connect():
                store.execute("UPDATE manual_operation_runs SET state='failed',next_poll=0,updated=? WHERE job_id=?",(store.now(),job['id']))
                store.finish(job['id'],'failed',error=str(failure))
            return True
        if record['status']!='succeeded':
            store.execute('UPDATE manual_operation_runs SET next_poll=?,failures=0 WHERE job_id=?',(store.now()+5,job['id']));return True
        phase='collect'
        store.execute("UPDATE manual_operation_runs SET state='collecting',updated=? WHERE job_id=?",(store.now(),job['id']))
        files=adapter.collect(w,s,record,lambda:host.stop.is_set() or store.job(job['id'])['status']=='cancelled')
        report=adapter.validate(w,s,files)
        if not report['passed']:raise ValueError('图片结果未通过技术验收')
        env=lease.runtime.env
        target=store.project_dir(job['project'])/'edit/jobs'/job['id']/lease.attempt
        target.mkdir(parents=True,mode=0o750);names=[];total=0
        for relative in files:
            source=env['workspace']/relative;name=source.name
            if name in names:raise ValueError('重复输出文件名')
            fd=os.open(source,os.O_RDONLY|os.O_NOFOLLOW|os.O_NONBLOCK)
            with os.fdopen(fd,'rb') as src,(target/name).open('xb') as dst:
                info=os.fstat(src.fileno())
                if not stat.S_ISREG(info.st_mode):raise ValueError('无效输出')
                from .egress_policy import copy_public
                copy_public(src,dst)
                total+=info.st_size
            os.chown(target/name,lease.runtime.service_uid,lease.runtime.service_gid);(target/name).chmod(0o640);names.append(name)
        parent=target
        while parent!=store.project_dir(job['project'])/'edit':
            os.chown(parent,lease.runtime.service_uid,lease.runtime.service_gid);parent.chmod(0o750);parent=parent.parent
        if store.shared_files:
            for name in names:store.artifacts.save_file(job['project'],str((target/name).relative_to(store.project_dir(job['project']))))
        with store.connect() as db:
            if store.job(job['id'])['status']=='cancelled':return True
            store.check_storage(job['project'],total)
            db.execute('INSERT INTO execution_job_results(job_id,attempt,bytes) VALUES (?,?,?) ON CONFLICT(job_id) DO UPDATE SET attempt=excluded.attempt,bytes=excluded.bytes',(job['id'],lease.attempt,total))
            store.finish(job['id'],'succeeded',names)
            db.execute("UPDATE manual_operation_runs SET state='succeeded',updated=? WHERE job_id=?",(store.now(),job['id']))
    except Exception:
        from .task_leases import LeaseLost
        from .task_lifecycle import TaskLimitReached
        import sys
        if isinstance(sys.exc_info()[1],(LeaseLost,TaskLimitReached)):raise
        if phase in ('poll','collect') and row['failures']<4:
            store.execute('UPDATE manual_operation_runs SET failures=failures+1,next_poll=? WHERE job_id=?',(store.now()+min(60,5*2**row['failures']),job['id']))
        else:
            error=sys.exc_info()[1]
            if phase=='submit':
                from .provider_calls import update
                calls=store.query('SELECT state FROM workflow_provider_calls WHERE request_key=?',(w['id']+':'+s['id']+':0',))
                if calls and calls[0]['state']=='submitting':update(store,w,s,'submission_unknown')
            from .task_leases import TaskQuotaReached
            code=getattr(error,'diagnostic',{}).get('code')
            rejected=code=='provider_rejected'
            with store.connect():
                if code=='pre_submit' or rejected or isinstance(error,TaskQuotaReached):
                    store.execute("UPDATE manual_operation_runs SET state='failed',next_poll=0,updated=? WHERE job_id=?",(store.now(),job['id']))
                store.finish(job['id'],'failed',error='调用额度已用完，本次未提交。' if isinstance(error,TaskQuotaReached) else '提交前检查失败，本次未发起生成。' if code=='pre_submit' else '上游拒绝请求，请核对参数或授权。' if rejected else '外部提交结果未知，未重提。' if phase=='submit' else '外部查询或结果验收失败，已保留任务 ID。')
    return True
