"""Independent node watchdog. Run as a separate service from AgentHost."""
import argparse
import fcntl
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import time
import ctypes
import pwd

from .execution_resources import IdentityPool,Cgroups,stop_guarded_processes
from .store import Store
from .task_leases import initialize
from .task_lifecycle import administrative
from .paths import data_root


def clean(pool,uid,attempt):
    registry=pool.root/'records'/f'{uid}.json'
    if not registry.exists():return 0
    record=json.loads(registry.read_text())
    if record['attempt']!=attempt:return 0
    fd=os.open(pool.root/'identities'/str(uid),os.O_RDWR|os.O_NOFOLLOW)
    try:
        try:fcntl.flock(fd,fcntl.LOCK_EX|fcntl.LOCK_NB)
        except BlockingIOError:
            # Stop generated code even if its coordinator is stuck. Do not free
            # the identity while that coordinator still owns its file lock.
            Cgroups.clear(record.get('cgroups',[]))
            stop_guarded_processes(record)
            return 3
        owner=record.get('mount_owner')
        if owner and record.get('boot_id')==Path('/proc/sys/kernel/random/boot_id').read_text().strip() and (Path('/proc')/str(owner)).exists():
            proc=Path('/proc')/str(owner)
            try:
                start=(proc/'stat').read_text().rsplit(')',1)[1].split()[19]
                namespace=os.open(proc/'ns/mnt',os.O_RDONLY)
            except FileNotFoundError:namespace=None
            if namespace is not None:
                try:
                    if start!=record.get('mount_owner_start') or (proc/'stat').read_text().rsplit(')',1)[1].split()[19]!=start:
                        raise RuntimeError('Mount namespace owner changed')
                    if os.fstat(namespace).st_ino!=os.stat('/proc/self/ns/mnt').st_ino:
                        if ctypes.CDLL(None,use_errno=True).setns(namespace,0)!=0:
                            raise OSError(ctypes.get_errno(),'Cannot enter task mount namespace')
                finally:os.close(namespace)
        pool.cleanup(record,uid)
        registry.unlink(missing_ok=True)
        os.ftruncate(fd,0);os.fsync(fd)
        return 0
    finally:os.close(fd)


class Reaper:
    def __init__(self,store,pool):
        self.store,self.pool=store,pool
        self.store.timeout=.1
        self.children={}
        self.external_children={}
        self.boot=Path('/proc/sys/kernel/random/boot_id').read_text().strip()
        try:initialize(store)
        except Exception as exc:
            from .telemetry import emit
            emit('reaper.observation_failed','WARNING',exc=exc)  # Local revocation must work even on a DB outage at startup.

    def mark(self,record,state,error=''):
        run=self.pool.root/'runs'/record['attempt']
        marker=run/'revoked'
        if not run.exists() and not (self.pool.root/'records'/f'{record["uid"]}.json').exists():state='complete';error=''
        with self.pool.control_lock(record['uid']):
            try:
                control=json.loads(marker.read_text())
                retries=control.get('retries',0)+1
                control.update(retries=retries,next_retry=time.monotonic()+min(300,5*2**min(retries-1,6)))
                temporary=marker.with_suffix('.new');temporary.write_text(json.dumps(control));temporary.replace(marker)
            except (OSError,ValueError):pass
        try:
            with administrative(),self.store.connect() as db:
                # A late cleanup receipt belongs to ONE attempt. Revoking every
                # attempt of this task can kill its already-started successor.
                timestamp=self.store.now()
                duration=max(1,float(os.environ.get('VIDEO_USE_CLEANUP_SECONDS','60')))
                db.execute("UPDATE execution_cleanup SET started=COALESCE(started,?),deadline_at=COALESCE(deadline_at,?) WHERE attempt=? AND state<>'complete'",
                           (timestamp,timestamp+duration,record['attempt']))
                db.execute("UPDATE execution_cleanup SET state=?,error=?,updated=?,retries=retries+1,next_retry=? WHERE attempt=? AND state<>'complete'",
                    (state,error,self.store.now(),self.store.now()+min(300,5*2**min(record.get('retries',0),6)),record['attempt']))
                if state=='quarantined':db.execute('UPDATE execution_workers SET draining=1 WHERE node=?',(self.pool.node,))
        except Exception as exc:
            from .telemetry import emit
            emit('reaper.observation_failed','WARNING',exc=exc)
        if state=='quarantined':
            (self.pool.root/'node-quarantined').write_text('Resource cleanup requires attention\n')
            from .telemetry import emit
            emit('cleanup.quarantined','ERROR',attempt_id=record['attempt'],node=self.pool.node,error_code='CleanupQuarantined')

    def launch(self,record):
        attempt=record['attempt']
        run=self.pool.root/'runs'/attempt
        with self.pool.control_lock(record['uid']):
            if run.is_dir():
                try:
                    with (run/'revoked').open('x') as stream:
                        deadline=time.monotonic()+float(os.environ.get('VIDEO_USE_CLEANUP_SECONDS','60'))
                        if record.get('boot_id')==self.boot:deadline=min(deadline,record.get('cleanup_deadline',deadline))
                        stream.write(json.dumps({'deadline':deadline,'boot':self.boot}))
                except FileExistsError:pass
        env={'PATH':'/usr/bin:/bin','PYTHONPATH':str(Path(__file__).resolve().parents[1]),
             'VIDEO_USE_EXECUTOR_ROOT':str(self.pool.root),'VIDEO_USE_UID_START':str(self.pool.start),'VIDEO_USE_UID_COUNT':str(self.pool.size)}
        for key in ('VIDEO_USE_DATABASE_URL','VIDEO_USE_DISTRIBUTED'):
            if key in os.environ:env[key]=os.environ[key]
        from .telemetry import child_env
        env.update(child_env('reaper_clean',record.get('task_id'),attempt))
        process=subprocess.Popen([sys.executable,'-m','webapp.execution_reaper','clean',str(record['uid']),attempt],
            env=env,stdin=subprocess.DEVNULL,stdout=subprocess.DEVNULL,stderr=None,start_new_session=True)
        self.children[attempt]=(process,time.monotonic(),record)

    def tick(self):
        # Run the local permit watchdog BEFORE touching the DB. A blocked or
        # unreachable metadata store must not extend a process's execution right.
        for path in (self.pool.root/'records').glob('*.json'):
            try:record=json.loads(path.read_text())
            except (OSError,ValueError):continue
            if not record.get('task_id') or record['attempt'] in self.children:continue
            marker=self.pool.root/'runs'/record['attempt']/'revoked'
            if marker.exists():
                try:
                    control=json.loads(marker.read_text())
                    if control.get('boot')==self.boot:
                        if time.monotonic()>control.get('deadline',float('inf')):
                            (self.pool.root/'node-quarantined').write_text('Cleanup deadline exceeded\n')
                        if time.monotonic()<control.get('next_retry',0):continue
                except (OSError,ValueError):pass
            if record.get('boot_id')!=self.boot or time.monotonic()>=record.get('permit_until',0) or record.get('cleanup_requested'):
                self.launch(record)
        try:
            from .task_lifecycle import sweep
            with self.store.connect() as db:
                db.execute('BEGIN IMMEDIATE');sweep(db)
        except Exception as exc:
            from .telemetry import emit
            emit('reaper.observation_failed','WARNING',exc=exc)
        for key,process in list(self.external_children.items()):
            if process.poll() is not None:del self.external_children[key]
        if len(self.external_children)<2:
            try:rows=self.store.query("SELECT id,task_id FROM execution_external_cleanup WHERE state IN ('pending','running') AND next_retry<=? AND lease_until<=? LIMIT 2",(self.store.now(),self.store.now()))
            except Exception as exc:
                rows=[]
                from .telemetry import emit
                emit('reaper.external_scan_failed','WARNING',exc=exc)
            if rows:
                from .gateway_process import grant_database
                grant_database(self.store)
                account=pwd.getpwnam('video-use')
                for row in rows:
                    if row['id'] in self.external_children:continue
                    env={'PATH':'/usr/bin:/bin','PYTHONPATH':str(Path(__file__).resolve().parents[1]),'PYTHONDONTWRITEBYTECODE':'1',
                         'VIDEO_USE_INTERNAL_PROVIDER_HOSTS':os.environ.get('VIDEO_USE_INTERNAL_PROVIDER_HOSTS','localhost,127.0.0.1,::1')}
                    for key in ('VIDEO_USE_DATABASE_URL','VIDEO_USE_DISTRIBUTED','VIDEO_USE_OBJECT_BUCKET','VIDEO_USE_GENERATION_URL','VIDEO_USE_GENERATION_KEY','VIDEO_USE_GENERATION_MODEL','VIDEO_USE_GENERATION_BACKEND','VIDEO_USE_GENERATION_DOWNLOAD_HOSTS','VIDEO_USE_IMAGE_VIDEO_URL','VIDEO_USE_IMAGE_VIDEO_KEY','VIDEO_USE_IMAGE_VIDEO_MODEL','VIDEO_USE_IMAGE_VIDEO_BACKEND','VIDEO_USE_IMAGE_VIDEO_DOWNLOAD_HOSTS'):
                        if os.environ.get(key):env[key]=os.environ[key]
                    from .telemetry import child_env
                    env.update(child_env('external_cleanup',row['task_id']))
                    self.external_children[row['id']]=subprocess.Popen(['/usr/bin/setpriv',f'--reuid={account.pw_uid}',f'--regid={account.pw_gid}',
                        '--clear-groups','--no-new-privs','--bounding-set=-all',sys.executable,'-m','webapp.external_cleanup',str(self.store.root),row['id']],
                        env=env,stdin=subprocess.DEVNULL,stdout=subprocess.DEVNULL,stderr=None)
        for attempt,(process,started,record) in list(self.children.items()):
            code=process.poll()
            if code is not None:
                if code==0:self.mark(record,'complete')
                elif code==3:self.mark(record,'cleaning','等待原执行器释放身份锁。')
                # A failed cleanup keeps this identity unavailable. Allow the
                # bounded retry window before draining the entire node; deadline
                # enforcement below remains fail-closed even without the DB.
                else:self.mark(record,'cleaning','资源清理暂未完成，身份保持隔离并等待重试。')
                del self.children[attempt]
            elif time.monotonic()-started>15:
                try:os.killpg(process.pid,signal.SIGKILL)
                except ProcessLookupError:pass
                self.mark(record,'quarantined','清理进程超时，未确认资源释放。')
                self.children[attempt]=(process,time.monotonic(),record)
        for path in (self.pool.root/'records').glob('*.json'):
            try:record=json.loads(path.read_text())
            except (OSError,ValueError):continue
            attempt=record.get('attempt')
            if not attempt or attempt in self.children or not record.get('task_id'):continue
            expired=record.get('boot_id')!=self.boot or time.monotonic()>=record.get('permit_until',0) or record.get('cleanup_requested',False)
            if record.get('cleanup_deadline') and time.monotonic()>record['cleanup_deadline']:
                self.mark(record,'quarantined','节点清理期限已到，身份保持隔离。')
            try:
                with self.store.connect() as db:
                    row=db.execute('SELECT * FROM execution_cleanup WHERE attempt=?',(attempt,)).fetchone()
                    task=db.execute('SELECT termination_reason FROM execution_limits WHERE task_id=?',(record['task_id'],)).fetchone()
                    lease=db.execute('SELECT attempt,active,expires FROM execution_leases WHERE attempt=?',(record['attempt'],)).fetchone()
                    expired |= bool(task and task['termination_reason']) or not lease or lease['attempt']!=attempt or not lease['active'] or lease['expires']<=self.store.now()
                    if row and row['state'] in ('pending','cleaning','quarantined'):expired=True
                    if row:
                        record['retries']=row['retries']
                        if row['deadline_at'] and self.store.now()>row['deadline_at'] and row['state'] not in ('complete','quarantined'):
                            self.mark(record,'quarantined','清理期限已到，节点已停止接单。')
                        if row['next_retry']>self.store.now():continue
            except Exception as exc:
                from .telemetry import emit
                emit('reaper.database_unavailable','WARNING',exc=exc,task_id=record['task_id'],attempt_id=attempt)
                # The local monotonic permit still expires during DB outage.
            if not expired:continue
            self.launch(record)
            self.mark(record,'cleaning')
        # Normal teardown can finish while the DB is unavailable. Reconcile
        # missing local runtime records after connectivity returns.
        with self.store.connect() as db:
            rows=db.execute("SELECT c.attempt,a.uid FROM execution_cleanup c JOIN execution_attempts a ON a.id=c.attempt WHERE c.node=? AND c.state<>'complete'",(self.pool.node,)).fetchall()
            for row in rows:
                if row['attempt'] in self.children or (self.pool.root/'runs'/row['attempt']).exists():continue
                if row['uid'] is not None:
                    record_path=self.pool.root/'records'/f'{row["uid"]}.json'
                    if record_path.exists():
                        try:registered=json.loads(record_path.read_text())
                        except (OSError,ValueError):continue
                        if registered['attempt']==row['attempt']:continue
                db.execute("UPDATE execution_cleanup SET state='complete',error='',updated=? WHERE attempt=?",(self.store.now(),row['attempt']))


def main():
    from .telemetry import setup,emit
    setup('watchdog')
    parser=argparse.ArgumentParser();parser.add_argument('action',choices=['run','once','clean'],nargs='?',default='run')
    parser.add_argument('uid',type=int,nargs='?');parser.add_argument('attempt',nargs='?');args=parser.parse_args()
    if os.geteuid()!=0:raise SystemExit('The node reaper must run as root')
    pool=IdentityPool(reconcile=False)
    if args.action=='clean':raise SystemExit(clean(pool,args.uid,args.attempt))
    reaper=Reaper(Store(data_root(),timeout=.1,initialize_schema=False),pool)
    while True:
        try:reaper.tick()
        except Exception as exc:emit('reaper.error','ERROR',exc=exc)
        if args.action=='once':break
        time.sleep(1)


if __name__=='__main__':main()
