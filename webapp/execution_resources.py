"""Node-local identity leases and hard task resource boundaries.

The root-owned registry is shared by every worker on a host. flock protects live
leases; orphan UIDs are quarantined until processes, mounts and files are gone.
"""
from __future__ import annotations

import fcntl
import json
import os
from pathlib import Path
import pwd
import grp
import shutil
import signal
import subprocess
import time
import threading
import uuid
import select
from contextlib import contextmanager
from .process_handles import open_pid,send_signal


def uid_alive(uid):
    for path in Path('/proc').glob('[0-9]*/status'):
        try:
            for line in path.read_text().splitlines():
                if line.startswith('Uid:') and uid in map(int, line.split()[1:]):
                    return True
        except (OSError, ValueError):
            continue
    return False


def stop_guarded_processes(record):
    if record.get('boot_id') and record['boot_id']!=Path('/proc/sys/kernel/random/boot_id').read_text().strip():return
    for child in record.get('guard_processes',[]):
        pid=int(child['pid'])
        try:descriptor=open_pid(pid)
        except ProcessLookupError:continue
        try:
            try:start=Path(f'/proc/{pid}/stat').read_text().rsplit(')',1)[1].split()[19]
            except FileNotFoundError:continue
            if start!=child['start']:continue
            try:send_signal(descriptor,signal.SIGKILL)
            except ProcessLookupError:continue
            if not select.select([descriptor],[],[],3)[0]:raise RuntimeError('Gateway process did not exit')
        finally:os.close(descriptor)


class Cgroups:
    def __init__(self, attempt, memory, cpus, pids):
        self.paths = []
        base = Path('/sys/fs/cgroup')
        if (base/'cgroup.controllers').exists():
            parent = base/'video-use'
            parent.mkdir(exist_ok=True)
            for folder in (base, parent):
                (folder/'cgroup.subtree_control').write_text('+cpu +memory +pids')
            path = parent/attempt
            path.mkdir()
            self.paths.append(path)
            (path/'memory.max').write_text(str(memory))
            (path/'memory.swap.max').write_text('0')
            (path/'pids.max').write_text(str(pids))
            (path/'cpu.max').write_text(f'{int(cpus*100000)} 100000')
            (path/'memory.oom.group').write_text('1')
        else:
            # cgroup v1 is still used on the current execution server.
            for controller in ('memory', 'cpu', 'pids'):
                parent = base/controller/'video-use'
                parent.mkdir(exist_ok=True)
                path = parent/attempt
                path.mkdir()
                self.paths.append(path)
                if controller == 'memory':
                    (path/'memory.limit_in_bytes').write_text(str(memory))
                    if (path/'memory.memsw.limit_in_bytes').exists():
                        (path/'memory.memsw.limit_in_bytes').write_text(str(memory))
                elif controller == 'cpu':
                    (path/'cpu.cfs_period_us').write_text('100000')
                    (path/'cpu.cfs_quota_us').write_text(str(int(cpus*100000)))
                else:
                    (path/'pids.max').write_text(str(pids))

    @staticmethod
    def clear(paths):
        for root in map(Path, paths):
            if not root.exists():continue
            directories=[Path(p) for p,_,_ in os.walk(root,topdown=False)]
            for path in directories:Cgroups.clear_one(path)

    @staticmethod
    def clear_one(path):
            if not path.exists():
                return
            if (path/'cgroup.kill').exists():
                (path/'cgroup.kill').write_text('1')
            for _ in range(50):
                members = (path/'cgroup.procs').read_text().split()
                if not members:
                    break
                for pid in members:
                    try:
                        descriptor=open_pid(int(pid))
                        try:
                            memberships=Path('/proc',pid,'cgroup').read_text()
                            if '/video-use/' in str(path) and '/video-use/'+str(path).split('/video-use/',1)[1] in memberships:
                                send_signal(descriptor,signal.SIGKILL)
                        finally:os.close(descriptor)
                    except ProcessLookupError:pass
                    except FileNotFoundError:pass
                time.sleep(.02)
            if (path/'cgroup.procs').read_text().strip():
                raise RuntimeError('Task processes did not exit; identity quarantined')


class Identity:
    def __init__(self, pool, descriptor, uid, record, path):
        self.pool, self.descriptor, self.uid, self.gid = pool, descriptor, uid, uid
        self.record, self.path = record, path
        self.lock=threading.Lock()
        self.closed=False

    def save(self, **values):
        with self.lock:
            if self.closed:return
            self.record.update(values)
            target=self.pool.root/'records'/f'{self.uid}.json'
            temporary=target.with_name(target.name+'.'+uuid.uuid4().hex)
            with temporary.open('x') as stream:
                stream.write(json.dumps(self.record));stream.flush();os.fsync(stream.fileno())
            temporary.chmod(0o600);temporary.replace(target)

    def close(self):
        with self.lock:
            if self.closed:return
            try:
                self.pool.cleanup(self.record, self.uid)
                (self.pool.root/'records'/f'{self.uid}.json').unlink(missing_ok=True)
                os.ftruncate(self.descriptor, 0)
                os.fsync(self.descriptor)
            finally:
                self.closed=True
                os.close(self.descriptor)


class IdentityPool:
    @contextmanager
    def control_lock(self, uid):
        # Separate from the lifetime identity lease: the watchdog must be able
        # to revoke a live owner, but must not recreate files during teardown.
        fd=os.open(self.root/'identities'/f'{int(uid)}.control',os.O_CREAT|os.O_RDWR|os.O_NOFOLLOW,0o600)
        try:
            fcntl.flock(fd,fcntl.LOCK_EX)
            yield
        finally:os.close(fd)

    def __init__(self, root=None, start=None, size=None, reconcile=True):
        self.root = Path(root or os.environ.get('VIDEO_USE_EXECUTOR_ROOT', '/var/lib/video-use-executor')).resolve()
        self.start = int(start or os.environ.get('VIDEO_USE_UID_START', '300000'))
        self.size = int(size or os.environ.get('VIDEO_USE_UID_COUNT', '128'))
        if self.start < 100000 or not 1 <= self.size <= 10000:
            raise ValueError('Invalid reserved task identity range')
        self.root.mkdir(mode=0o700, parents=True, exist_ok=True)
        if self.root.stat().st_uid != 0 or self.root.stat().st_mode & 0o077:
            raise RuntimeError('Executor registry must be root-owned with mode 0700')
        (self.root/'identities').mkdir(mode=0o700, exist_ok=True)
        (self.root/'records').mkdir(mode=0o700, exist_ok=True)
        (self.root/'runs').mkdir(mode=0o700, exist_ok=True)
        identity = self.root/'node-id'
        try:
            fd = os.open(identity, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        except FileExistsError:pass
        else:
            import uuid
            with os.fdopen(fd, 'w') as f:f.write(uuid.uuid4().hex)
        self.node = identity.read_text().strip()
        if reconcile:self.reconcile()

    def reconcile(self):
        for path in (self.root/'identities').iterdir():
            if not path.name.isdigit():continue
            uid=int(path.name)
            if not self.start<=uid<self.start+self.size:continue
            fd=os.open(path,os.O_RDWR|os.O_NOFOLLOW)
            try:
                try:fcntl.flock(fd,fcntl.LOCK_EX|fcntl.LOCK_NB)
                except BlockingIOError:continue
                record=self.root/'records'/f'{uid}.json'
                raw=record.read_bytes() if record.exists() else os.read(fd,65536)
                if raw:
                    try:self.cleanup(json.loads(raw),uid)
                    except Exception:continue  # Preserve the quarantined record.
                    os.ftruncate(fd,0);os.fsync(fd)
                    record.unlink(missing_ok=True)
            finally:os.close(fd)

    def cleanup(self, record, uid):
        paths = record.get('cgroups', [])
        for path in paths:
            if not str(path).startswith('/sys/fs/cgroup/') or Path(path).name != record.get('attempt'):
                raise RuntimeError('Invalid persisted cgroup')
        Cgroups.clear(paths)
        stop_guarded_processes(record)
        if uid_alive(uid):
            raise RuntimeError('UID still has live processes; quarantined')
        owner=record.get('mount_owner')
        if owner and record.get('boot_id')==Path('/proc/sys/kernel/random/boot_id').read_text().strip() and (Path('/proc')/str(owner)/'ns/mnt').exists():
            proc=Path('/proc')/str(owner)
            start=(proc/'stat').read_text().rsplit(')',1)[1].split()[19]
            if start==record.get('mount_owner_start') and (proc/'ns/mnt').stat().st_ino!=Path('/proc/self/ns/mnt').stat().st_ino:
                raise RuntimeError('Resource cleanup requires the node reaper mount namespace')
        if record.get('attempt'):
            path = self.root/'runs'/record['attempt']
            if len(record['attempt']) != 32 or any(c not in '0123456789abcdef' for c in record['attempt']):
                raise RuntimeError('Invalid attempt directory')
            if os.path.ismount(path/'data'):
                subprocess.run(['umount', str(path/'data')], check=True, capture_output=True,timeout=10)
            with self.control_lock(uid):
                if path.exists():shutil.rmtree(path)
            if record.get('gateway_temp'):
                gateway=Path(record['gateway_temp'])
                if gateway!=Path('/run/video-use-agent/client-temp')/record['attempt']:raise RuntimeError('Invalid gateway cleanup path')
                if os.path.ismount(gateway):subprocess.run(['umount',str(gateway)],check=True,capture_output=True,timeout=10)
                if gateway.exists():gateway.rmdir()
        for path in map(Path, paths):
            if path.exists():
                for directory,_,_ in os.walk(path,topdown=False):Path(directory).rmdir()

    def acquire(self, attempt):
        if (self.root/'node-quarantined').exists():raise RuntimeError('执行节点正在处理资源回收异常，已停止接单。')
        if len(attempt)!=32 or any(c not in '0123456789abcdef' for c in attempt):
            raise ValueError('Invalid execution attempt')
        reserved=[]
        for filename in ('/etc/subuid','/etc/subgid'):
            path=Path(filename)
            if path.exists():
                for line in path.read_text().splitlines():
                    parts=line.split(':')
                    if len(parts)==3 and parts[1].isdigit() and parts[2].isdigit():
                        reserved.append((int(parts[1]),int(parts[1])+int(parts[2])))
        for uid in range(self.start, self.start+self.size):
            if any(start<=uid<end for start,end in reserved):continue
            try:pwd.getpwuid(uid)
            except KeyError:pass
            else:continue
            try:grp.getgrgid(uid)
            except KeyError:pass
            else:continue
            fd = os.open(self.root/'identities'/str(uid), os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                os.close(fd)
                continue
            try:
                registry=self.root/'records'/f'{uid}.json'
                raw = registry.read_bytes() if registry.exists() else os.read(fd, 65536)
                record = json.loads(raw) if raw else {}
                self.cleanup(record, uid)
            except Exception:
                os.close(fd)
                continue  # Fail closed; an operator can inspect this slot.
            path = self.root/'runs'/attempt
            path.mkdir(mode=0o700)
            identity = Identity(self, fd, uid, {}, path)
            identity.save(attempt=attempt, uid=uid, cgroups=[])
            return identity
        raise RuntimeError('No clean execution identities available')
