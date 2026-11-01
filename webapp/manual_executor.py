"""Manual jobs use the same identities, leases, isolation and publication fence."""
import json
import os
from pathlib import Path
import shutil
import stat
import subprocess
import time

from . import agent_sandbox
from .agent_gateway import Gateway
from .paths import SANDBOX_WORKSPACE


def run(host, lease):
    from .agent_host import Shutdown, terminate
    store, runtime = host.store, lease.runtime
    job=store.job(lease.turn_id)
    if job['status']=='cancelled':return
    with store.connect() as db:
        if not db.execute("UPDATE jobs SET status='running' WHERE id=? AND status IN ('queued','running')",(job['id'],)).rowcount:return
    env=agent_sandbox.prepare(store,job['project'],host.model,'execute',job['id'])
    env['manual']=True
    output_name='manual-'+lease.attempt
    output=env['workspace']/'edit'/output_name
    output.mkdir(exist_ok=True);os.chown(output,env['uid'],env['gid'])
    manifest=env['workspace']/'manual-job.json'
    manifest.write_text(json.dumps({'project_dir':SANDBOX_WORKSPACE,
        'output_dir':SANDBOX_WORKSPACE+'/edit/'+output_name,'payload':job['payload'],'assets':env['assets']}))
    os.chown(manifest,env['uid'],env['gid']);manifest.chmod(0o600)
    socket_path=host.socket_dir/(lease.attempt+'.sock')
    def extract(filename,track,destination):
        with destination.open('wb') as stream:
            subprocess.run(agent_sandbox.command(env,socket_path,['ffmpeg','-v','error','-i',SANDBOX_WORKSPACE+'/sources/'+filename,
                '-map',f'0:a:{track}','-vn','-ac','1','-ar','16000','-c:a','pcm_s16le','-f','wav','pipe:1']),
                stdout=stream,stderr=subprocess.PIPE,timeout=300,check=True)
    gateway=Gateway(socket_path,env['project'],env['assets'],'execute',host.model,host.base_url,host.key,
                    env['uid'],env['gid'],lambda *_:None,extract,env['workspace'])
    process=None;target=None;published=False
    try:
        gateway.start()
        with (runtime.path/'data'/'job.log').open('w+b') as log:
            process=subprocess.Popen(agent_sandbox.command(env,socket_path,
                ['/opt/venv/bin/python','/opt/sandbox_entry.py','--command','/opt/venv/bin/python',
                 '-P','-m','webapp.worker',SANDBOX_WORKSPACE+'/manual-job.json']),
                stdout=log,stderr=subprocess.STDOUT,start_new_session=True,
                env={'PATH':'/usr/bin:/bin','LANG':'C.UTF-8'})
            deadline=time.monotonic()+7200
            while process.poll() is None:
                lease.check()
                if host.stop.is_set():raise Shutdown()
                if store.job(job['id'])['status']=='cancelled':return
                if time.monotonic()>deadline or os.fstat(log.fileno()).st_size>64*1024**2:
                    raise RuntimeError('任务超过执行时间或日志大小限制')
                time.sleep(.2)
            runtime.quiesce()
            log.seek(max(0,os.fstat(log.fileno()).st_size-65536))
            tail=host.redact(log.read().decode(errors='replace'))
            from .telemetry import process_exit,child_failure
            process_exit(process,runtime.groups.paths)
            if process.returncode:
                child_failure(runtime.path/'data'/'job.log')
                raise RuntimeError(tail[-3000:])
        gateway.close();gateway=None
        result=output/'result.json'
        if result.is_symlink() or result.stat().st_size>65536:raise ValueError('Invalid result manifest')
        from .operation_result import Result
        names=[item.path for item in Result.model_validate_json(result.read_text()).outputs]
        if not isinstance(names,list) or not 1<=len(names)<=40:raise ValueError('Invalid result list')
        if any(not isinstance(name,str) or Path(name).name!=name or name in ('.','..') for name in names):
            raise ValueError('Invalid output path')
        agent_sandbox.validate_generated(env,[output_name+'/'+name for name in names])
        target=env['project']/'edit'/'jobs'/job['id']/lease.attempt
        target.mkdir(parents=True)
        # Service-owned immutable output directory, never mounted into a task.
        parent=target
        while parent != env['project']/'edit':
            os.chown(parent,runtime.service_uid,runtime.service_gid);parent.chmod(0o750)
            parent=parent.parent
        total=0
        for name in names:
            if not isinstance(name,str) or Path(name).name!=name or name in ('.','..'):
                raise ValueError('Invalid output path')
            source=output/name
            info=source.lstat()
            if not stat.S_ISREG(info.st_mode) or info.st_nlink!=1:raise ValueError('Invalid output file')
            total+=info.st_size
            if total>4*1024**3:raise ValueError('Result quota exceeded')
            from .egress_policy import copy_public
            with source.open('rb') as stream,(target/name).open('xb') as destination:copy_public(stream,destination)
            os.chown(target/name,runtime.service_uid,runtime.service_gid);(target/name).chmod(0o640)
        (target/'job.log').write_text(tail)
        os.chown(target/'job.log',runtime.service_uid,runtime.service_gid);(target/'job.log').chmod(0o640)
        if store.shared_files:
            for name in [*names,'job.log']:
                store.artifacts.save_file(job['project'],str((target/name).relative_to(env['project'])))
        with store.connect() as db:
            if db.execute('SELECT status FROM jobs WHERE id=?',(job['id'],)).fetchone()['status']=='cancelled':return
            store.check_storage(job['project'],total)
            db.execute('INSERT INTO execution_job_results(job_id,attempt,bytes) VALUES (?,?,?) ON CONFLICT(job_id) DO UPDATE SET attempt=excluded.attempt,bytes=excluded.bytes',(job['id'],lease.attempt,total))
            store.finish(job['id'],'succeeded',names)
        published=True
    except Shutdown:raise
    except Exception as exc:
        from .telemetry import emit
        emit('manual.failed','ERROR',exc=exc)
        from .task_leases import LeaseLost
        if isinstance(exc,LeaseLost):raise
        with store.connect() as db:
            if db.execute('SELECT status FROM jobs WHERE id=?',(job['id'],)).fetchone()['status']!='cancelled':
                store.finish(job['id'],'failed',error=host.redact(str(exc))[-3000:])
    finally:
        runtime.begin_cleanup()
        if process:terminate(process)
        if gateway:gateway.close()
        if target and not published:shutil.rmtree(target,ignore_errors=True)
