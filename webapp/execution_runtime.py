"""Attempt-private files, bounded scratch storage, immutable durable checkpoints."""
from __future__ import annotations

from .deployment_identity import service_user, runtime_group, socket_root

import hashlib
import json
import os
from pathlib import Path
import pwd
import grp
import shutil
import stat
import subprocess
import sys
import uuid
import time

from .execution_resources import Cgroups
from .execution_metrics import measured
from .live_activity import observed


def copy_tree(source, destination, uid, gid, limit, manifest=None, include=None, predicate=None):
    """Copy regular files using directory descriptors; never follow agent links."""
    records, size = {}, 0
    destination.mkdir(mode=0o700, parents=True, exist_ok=True)
    os.chown(destination, uid, gid)
    if not source.exists():return records
    if source.is_symlink() or not source.is_dir():raise RuntimeError('Invalid checkpoint directory')
    for directory, dirs, names, fd in os.fwalk(source, follow_symlinks=False):
        rel = Path(directory).relative_to(source)
        target = destination/rel
        target.mkdir(mode=0o700, parents=True, exist_ok=True)
        os.chown(target, uid, gid)
        for name in dirs[:]:
            if not stat.S_ISDIR(os.stat(name, dir_fd=fd, follow_symlinks=False).st_mode):
                dirs.remove(name)
        for name in names:
            if predicate is not None and not predicate((rel/name).as_posix()):continue
            if include is not None and (rel/name).as_posix() not in include:continue
            info = os.stat(name, dir_fd=fd, follow_symlinks=False)
            if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
                continue
            if len(records) >= 20000 or size + info.st_size > limit:
                raise RuntimeError('Checkpoint exceeds configured file or byte limit')
            handle = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=fd)
            digest = hashlib.sha256()
            with os.fdopen(handle, 'rb') as src, (target/name).open('xb') as dst:
                current = os.fstat(src.fileno())
                if (current.st_dev, current.st_ino, current.st_size) != (info.st_dev, info.st_ino, info.st_size):
                    raise RuntimeError('Checkpoint changed while being copied')
                copied = 0
                while chunk := src.read(1024*1024):
                    copied += len(chunk)
                    if copied > info.st_size:raise RuntimeError('Checkpoint changed while being copied')
                    digest.update(chunk)
                    dst.write(chunk)
                if copied != info.st_size:raise RuntimeError('Incomplete checkpoint file')
                os.fchown(dst.fileno(), uid, gid)
                os.fchmod(dst.fileno(), 0o600 | (info.st_mode & 0o100))
                dst.flush()
                os.fsync(dst.fileno())
            key = (rel/name).as_posix()
            record = {'size': info.st_size, 'sha256': digest.hexdigest()}
            if manifest is not None and manifest.get(key) != record:
                raise RuntimeError('Checkpoint integrity check failed')
            records[key] = record
            size += info.st_size
    if manifest is not None and set(records) != set(manifest):
        raise RuntimeError('Checkpoint files are missing')
    return records


class TaskRuntime:
    @measured('prepare_environment')
    def __init__(self, lease, pool):
        self.lease, self.store = lease, lease.store
        lease.start_compute()
        self.identity = pool.acquire(lease.attempt)
        self.path = self.identity.path
        self.uid, self.gid = self.identity.uid, self.identity.gid
        self.env = None
        self.groups = None
        self.closed=False
        self.cancel_call=None
        lease.runtime=self
        self.limit = int(os.environ.get('VIDEO_USE_TASK_DISK_BYTES', str(8*1024**3)))
        service = pwd.getpwnam(service_user())
        self.service_uid, self.service_gid = service.pw_uid, service.pw_gid
        try:
            with self.store.connect() as db:
                expires=db.execute('SELECT expires FROM execution_leases WHERE attempt=?',(lease.attempt,)).fetchone()['expires']
                permit_seconds=max(0,expires-self.store.now())
            self.identity.save(task_id=lease.run_id,node=pool.node,database=str(self.store.root),
                    generation=lease.generation,worker=lease.worker,boot_id=Path('/proc/sys/kernel/random/boot_id').read_text().strip(),
                    permit_until=time.monotonic()+permit_seconds,
                    mount_owner=os.getpid(),mount_owner_start=Path('/proc/self/stat').read_text().rsplit(')',1)[1].split()[19])
            with self.store.connect() as db:
                timestamp=self.store.now()
                db.execute("INSERT INTO execution_cleanup(attempt,task_id,node,state,updated) VALUES (?,?,?,'allocated',?)",
                           (lease.attempt,lease.run_id,pool.node,timestamp))
            self.read_gid = grp.getgrnam(runtime_group()).gr_gid
            from .scheduling import requirements
            resources=requirements(self.store,lease.run_id,lease.kind,step_id=lease.step_id)
            memory_limit=min(int(os.environ.get('VIDEO_USE_TASK_MEMORY_BYTES',str(12*1024**3))),max(512,resources['memory_mb'])*1024**2)
            cpu_limit=min(float(os.environ.get('VIDEO_USE_TASK_CPUS','2')),max(1,resources['cpu']))
            self.groups = Cgroups.__new__(Cgroups)
            self.groups.paths = []
            try:
                self.groups.__init__(lease.attempt,
                    memory_limit,
                    cpu_limit,
                    int(os.environ.get('VIDEO_USE_TASK_PIDS', '128')))
            finally:
                self.identity.save(cgroups=[str(p) for p in self.groups.paths])
            from .cancellation import for_store
            self.cancel_call=for_store(self.store).register_lease(lease,'runtime')
            def stop_runtime():
                lease.lost.set()
                self.quiesce()
            self.cancel_call.install(stop_runtime)
            self.cancel_call.check()
            data = self.path/'data'
            data.mkdir(mode=0o700)
            storage=os.environ.get('VIDEO_USE_WORKSPACE_STORAGE','tmpfs')
            if storage=='tmpfs':
                subprocess.run(['mount', '-t', 'tmpfs', '-o',
                                f'size={self.limit},nr_inodes=50000,mode=0700,nosuid,nodev',
                                'tmpfs', str(data)], check=True, capture_output=True)
            elif storage=='disk':
                disk=self.path/'workspace.ext4'
                with disk.open('xb') as image:
                    os.posix_fallocate(image.fileno(),0,self.limit)
                subprocess.run(['mkfs.ext4','-F','-q','-E','nodiscard',str(disk)],check=True,capture_output=True,timeout=60)
                subprocess.run(['mount','-t','ext4','-o','loop,nosuid,nodev',str(disk),str(data)],check=True,capture_output=True,timeout=30)
                data.chmod(0o700)
            else:raise ValueError('VIDEO_USE_WORKSPACE_STORAGE must be tmpfs or disk')
            self.manifest = self.path/'execution.json'
            self.manifest.write_text(json.dumps({'cgroups': [str(p) for p in self.groups.paths],
                'cpus':float(os.environ.get('VIDEO_USE_TASK_CPUS','2')),
                'placement':int(lease.attempt[:8],16)}))
            self.manifest.chmod(0o600)
            self.agent = self.path/'agent'
            self.agent.mkdir(mode=0o700)
            self.workspace, self.state = data/'workspace', data/'state'
            table = 'execution_subjects'
            turn = self.store.query(f'SELECT project FROM {table} WHERE id=?', (lease.run_id,))[0]
            self.project = self.store.project_dir(turn['project'])
            from .workflow_graph import get as graph_get
            dynamic=graph_get(self.store,lease.run_id)
            allowed_scripts=None
            if dynamic:
                from .workflow_graph import work_item
                selected=work_item(dynamic,lease.step_id)
                self.dynamic_context={'run_id':dynamic['id'],'task_id':selected['id'],'goal':dynamic['goal']}
                self.source_asset_ids=(set(dynamic['authorization']['allowed_assets']) if selected.get('control_kind') else
                    {ref['asset'] for ref in selected['spec'].get('inputs',{}).values() if ref.get('asset')})
                spec=selected['spec']
                allowed_scripts=set(spec.get('preflight',[])+spec.get('config_files',[])+
                    [a for a in spec.get('argv',[]) if a.startswith('edit/') and a.endswith(('.py','.json','.sh'))])
            from .workspace_state import checkpoint_rows,seed_manifest
            checkpoint=checkpoint_rows(self.store,turn['project']);initial_workspace=None
            if checkpoint:
                source = self.store.root/checkpoint[0]['path']
                expected = json.loads(checkpoint[0]['manifest'])
                if dynamic:
                    expected={'workspace':{name:record for name,record in expected.get('workspace',{}).items() if name in allowed_scripts},'state':{}}
                from .session_checkpoint import persistent,BUNDLE
                if BUNDLE not in expected.get('state',{}):
                    expected['state']={name:record for name,record in expected.get('state',{}).items() if persistent(name)}
                if self.store.shared_files:
                    for group, files in expected.items():
                        for relative, record in files.items():
                            self.store.artifacts.materialize(turn['project'],record['artifact'],source/group/relative)
                    expected={group:{name:{k:v for k,v in record.items() if k not in ('artifact','format')} for name,record in files.items()}
                              for group,files in expected.items()}
            else:
                initial_workspace=seed_manifest(self.store,turn['project'])
                if dynamic:initial_workspace={name:record for name,record in initial_workspace.items() if name in allowed_scripts}
                source = self.project/'agent'
                if self.store.shared_files:
                    for relative,record in initial_workspace.items():
                        self.store.artifacts.materialize(turn['project'],record['artifact'],source/'workspace'/relative)
                initial_workspace={name:{k:record[k] for k in ('size','sha256')} for name,record in initial_workspace.items()}
                expected = None
            restored_state=None
            from .session_checkpoint import BUNDLE,FORMAT,unpack
            if checkpoint and not dynamic:
                session=json.loads(checkpoint[0]['manifest']).get('state',{})
                if BUNDLE in session and session[BUNDLE].get('format')==FORMAT:
                    if set(session)!={BUNDLE}:raise ValueError('Mixed session checkpoint format')
                    restored_state=self.path/'restored-session'
                    # materialize already checked the outer hash in distributed
                    # storage; verify it locally as well before opening the archive.
                    bundle=source/'state'/BUNDLE
                    with bundle.open('rb') as stream:
                        if hashlib.file_digest(stream,'sha256').hexdigest()!=session[BUNDLE]['sha256']:raise ValueError('Session bundle hash mismatch')
                    expected['state']=unpack(bundle,restored_state,self.limit)
            for name, target in (('workspace', self.workspace), ('state', self.state)):
                if name=='state':
                    from .checkpoints import state_compatible
                    if dynamic or not checkpoint or not state_compatible(self.store,turn['project'],checkpoint[0]['path']):
                        target.mkdir(mode=0o700);os.chown(target,self.uid,self.gid)
                        continue
                manifest=expected[name] if expected else initial_workspace if name=='workspace' else None
                include=list(manifest) if manifest is not None and (dynamic or name=='state' or not checkpoint) else None
                self.stage(restored_state if name=='state' and restored_state else source/name,target,manifest,include=include)
            from .step_checkpoints import restore as restore_steps
            restore_steps(self)
            old_prefix=str(Path(__file__).resolve().parents[1]/'workspace')
            self.legacy_paths=any(old_prefix in p.read_text(errors='replace') for p in self.workspace.rglob('*.py') if p.stat().st_size<2*1024**2)
            # Source permissions remain unchanged. Copies grant this attempt only
            # access to its own project and consume its bounded scratch allocation.
            self.sources, self.transcripts = data/'sources', data/'transcripts'
            assets=[a for a in self.store.assets(self.project.name) if not dynamic or a['id'] in self.source_asset_ids]
            if dynamic and self.store.shared_files:
                for asset in assets:self.store.file(turn['project'],'sources/'+asset['filename'])
                stems={Path(a['filename']).stem for a in assets}|{a['id'] for a in assets}
                for row in self.store.query("SELECT path FROM artifact_files WHERE project=? AND path LIKE 'edit/transcripts/%'",(turn['project'],)):
                    name=Path(row['path']).name
                    if any(name==stem+'.json' or name.startswith(stem+'.track') for stem in stems):self.store.file(turn['project'],row['path'])
            else:
                self.store.restore_files(turn['project'],'sources')
                self.store.restore_files(turn['project'],'edit/transcripts')
            self.stage(self.project/'sources', self.sources,include=[a['filename'] for a in assets])
            transcript_names=None
            if dynamic:
                stems={Path(a['filename']).stem for a in assets}|{a['id'] for a in assets}
                transcript_names=[p.name for p in (self.project/'edit/transcripts').glob('*.json') if any(p.name==stem+'.json' or p.name.startswith(stem+'.track') for stem in stems)]
            self.stage(self.project/'edit/transcripts', self.transcripts,include=transcript_names)
            self.preview_root = data/'previews'
            self.preview_root.mkdir(mode=0o700)
            os.chown(self.preview_root, self.uid, self.gid)
            with self.store.connect() as db:
                db.execute('UPDATE execution_attempts SET runtime=?,uid=? WHERE id=?',
                           (str(self.path), self.uid, lease.attempt))
        except Exception:
            self.close()
            lease.runtime=None
            raise

    def revoked(self):return (self.path/'revoked').exists()

    def pulse(self,seconds):
        if not self.closed:self.identity.save(permit_until=time.monotonic()+max(0,seconds))

    def begin_cleanup(self):
        from .task_lifecycle import administrative,request_cleanup
        self.identity.save(cleanup_requested=True,
            cleanup_deadline=self.identity.record.get('cleanup_deadline') or time.monotonic()+max(1,float(os.environ.get('VIDEO_USE_CLEANUP_SECONDS','60'))))
        try:
            with administrative(),self.store.connect() as db:request_cleanup(db,self.lease.run_id,attempt_id=self.lease.attempt,external=False)
        except Exception:pass

    def stage(self, source, destination, manifest=None, include=None):
        request=self.path/'copy-request.json'
        result=self.path/'copy-result.json'
        request.write_text(json.dumps({'source':str(source),'destination':str(destination),
            'uid':self.uid,'gid':self.gid,'limit':self.limit,'manifest':manifest,'include':include,'result':str(result)}))
        request.chmod(0o600)
        repo=Path(__file__).resolve().parents[1]
        process=subprocess.Popen([sys.executable,str(Path(__file__).with_name('execution_entry.py')),str(self.manifest),
            sys.executable,'-m','webapp.execution_copy',str(request)],
            cwd=repo,env={'PATH':'/usr/bin:/bin','PYTHONPATH':str(repo),'PYTHONDONTWRITEBYTECODE':'1'},
            stdout=subprocess.DEVNULL,stderr=subprocess.PIPE)
        deadline=time.monotonic()+300
        try:
            while process.poll() is None:
                self.lease.check()
                table='execution_subjects'
                status=self.store.query(f'SELECT status FROM {table} WHERE id=?',(self.lease.run_id,))[0]['status']
                if (status in ('cancelled','cancelling') and self.lease.kind!='workflow') or time.monotonic()>deadline:
                    raise RuntimeError('Task preparation cancelled or timed out')
                time.sleep(.05)
            if process.returncode:raise RuntimeError('任务输入超过资源限制或检查点不完整，无法启动。')
            self.lease.check()
        finally:
            if process.poll() is None:self.quiesce();process.wait(timeout=5)
            if process.stderr:process.stderr.close()

    def scope(self):
        name=uuid.uuid4().hex
        paths=[path/name for path in self.groups.paths]
        for path in paths:path.mkdir()
        manifest=self.path/(name+'.json')
        config=json.loads(self.manifest.read_text());config['cgroups']=list(map(str,paths))
        manifest.write_text(json.dumps(config));manifest.chmod(0o600)
        return {'paths':paths,'manifest':manifest,'stopped':True}

    def close_scope(self,scope):
        Cgroups.clear(scope['paths'])
        for path in scope['paths']:
            if path.exists():path.rmdir()
        scope['stopped']=True

    def wrap(self, command,scope=None):
        self.lease.check()
        return [sys.executable, str(Path(__file__).with_name('execution_entry.py')),
                str(scope['manifest'] if scope else self.manifest), *command]

    def quiesce(self):
        if self.groups:Cgroups.clear(self.groups.paths)

    @observed('host','checkpoint','保存成果与会话检查点')
    def checkpoint(self):
        self.lease.check()
        if self.lease.kind=='workflow' and self.lease.step_id:
            from .step_checkpoints import save
            return save(self)
        self.quiesce()
        folder = self.project/'execution-checkpoints'
        folder.mkdir(mode=0o700, exist_ok=True)
        os.chown(folder,self.service_uid,self.service_gid)
        target = folder/(self.lease.attempt+'-'+uuid.uuid4().hex)
        target.mkdir(mode=0o700)
        os.chown(target,self.service_uid,self.service_gid)
        try:
            from .session_checkpoint import persistent,pack
            records = {name: copy_tree(source, target/name, self.service_uid, self.service_gid, self.limit,
                                      predicate=persistent if name=='state' else None)
                       for name, source in (('workspace', self.workspace), ('state', self.state))}
            if self.store.shared_files:
                records['state']=pack(target/'state',records['state'])
                for name in records['state']:os.chown(target/'state'/name,self.service_uid,self.service_gid)
            prior=self.store.query('SELECT * FROM execution_checkpoints WHERE project=?',(self.project.name,))
            previous=json.loads(prior[0]['manifest'])['workspace'] if prior else {}
            if self.store.shared_files:
                for name,record in previous.items():
                    self.store.artifacts.materialize(self.project.name,record['artifact'],self.store.root/prior[0]['path']/'workspace'/name)
                previous={name:{k:v for k,v in record.items() if k!='artifact'} for name,record in previous.items()}
            blocked=[name for name in previous if name.startswith('edit/') and name not in records['workspace']]
            if blocked:
                restored=copy_tree(self.store.root/prior[0]['path']/'workspace',target/'workspace',self.service_uid,self.service_gid,self.limit,
                                   {k:previous[k] for k in blocked},blocked)
                records['workspace'].update(restored)
            permitted={'assets.json','image-document.json','workflow-state.json','continuation.json','capabilities.json','conversation.jsonl','harness-context.json'}
            rejected=[name for name in records['workspace'] if not name.startswith('edit/') and name not in permitted]
            # These read-only indexes are rebuilt from authoritative project
            # metadata by prepare(); their workspace inodes are only placeholders.
            regenerated=[name for name in records['workspace'] if name in permitted]
            for name in rejected+regenerated:
                (target/'workspace'/name).unlink();del records['workspace'][name]
            changes={'added':[k for k in records['workspace'] if k not in previous],
                     'modified':[k for k,v in records['workspace'].items() if k in previous and v!=previous[k]],
                     'blocked_deletions':blocked,'blocked_writes':rejected}
            if self.store.shared_files:
                for group,files in records.items():
                    for name,record in files.items():
                        # Reuse only same-project objects with live references;
                        # such objects cannot be swept while this lease is active.
                        known=self.store.query("SELECT o.id FROM artifact_objects o WHERE o.project=? AND o.sha256=? AND o.size=? AND o.state='published' AND EXISTS (SELECT 1 FROM artifact_files f WHERE f.artifact=o.id) LIMIT 1",
                                               (self.project.name,record['sha256'],record['size']))
                        if known:record['artifact']=known[0]['id']
                        else:
                            uploaded=self.store.artifacts.upload(self.project.name,target/group/name)
                            if uploaded['sha256']!=record['sha256']:raise RuntimeError('Checkpoint changed before upload')
                            record['artifact']=uploaded['id']
            for directory,_,_ in os.walk(target,topdown=False):
                fd=os.open(directory,os.O_RDONLY|os.O_DIRECTORY)
                try:os.fsync(fd)
                finally:os.close(fd)
            fd=os.open(folder,os.O_RDONLY|os.O_DIRECTORY)
            try:os.fsync(fd)
            finally:os.close(fd)
            # Copy finishes before the short transaction; takeover can never
            # publish a stale pointer, and orphan directories are not checkpoints.
            with self.store.connect() as db:
                from .task_leases import now
                self.lease.validate(db)
                from .checkpoints import profile
                old=db.execute('SELECT * FROM execution_checkpoints WHERE project=?',(self.project.name,)).fetchone()
                if old:
                    db.execute('INSERT OR IGNORE INTO execution_checkpoint_versions VALUES (?,?,?,?,?,?,?,?,?)',
                        (uuid.uuid4().hex,self.project.name,old['attempt'],old['path'],old['manifest'],'{}','legacy',None,old['updated']))
                history=db.execute('SELECT id,path FROM execution_checkpoint_versions WHERE project=? ORDER BY created DESC',(self.project.name,)).fetchall()
                keep=max(2,min(10,int(os.environ.get('VIDEO_USE_CHECKPOINT_VERSIONS','3'))))
                retained=[r for r in history if old and r['path']==old['path']]
                retained += [r for r in history if r not in retained][:max(0,keep-1-len(retained))]
                expired=[r for r in history if r not in retained]
                for item in expired:
                    db.execute('DELETE FROM execution_checkpoint_versions WHERE id=?',(item['id'],))
                    db.execute('DELETE FROM execution_native_descriptors WHERE project=? AND path=?',(self.project.name,item['path']))
                    if self.store.shared_files:
                        old_path=self.store.root/item['path']
                        if old_path.parent==folder:
                            self.store.artifacts.release_prefix(self.project.name,str(old_path.relative_to(self.project)))
                self.store.check_storage(self.project.name,
                    sum(item['size'] for group in records.values() for item in group.values()))
                relative = str(target.relative_to(self.store.root))
                if self.store.shared_files:
                    for group,files in records.items():
                        for name,record in files.items():
                            logical=str((target/group/name).relative_to(self.project))
                            self.store.artifacts.publish(self.project.name,logical,record['artifact'],expected_revision=0)
                db.execute('INSERT INTO execution_checkpoints VALUES (?,?,?,?,?) ON CONFLICT(project) DO UPDATE SET attempt=excluded.attempt,path=excluded.path,manifest=excluded.manifest,updated=excluded.updated',
                           (self.project.name, self.lease.attempt, relative, json.dumps(records), now(db)))
                db.execute('UPDATE execution_attempts SET checkpoint=? WHERE id=?', (relative, self.lease.attempt))
                thread=db.execute('SELECT thread_id FROM agent_sessions WHERE project=?',(self.project.name,)).fetchone()
                db.execute('INSERT INTO execution_checkpoint_versions VALUES (?,?,?,?,?,?,?,?,?)',
                    (uuid.uuid4().hex,self.project.name,self.lease.attempt,relative,json.dumps(records),json.dumps(changes),profile(),thread['thread_id'] if thread else None,now(db)))
                from .checkpoints import save_native_descriptor
                save_native_descriptor(self.store,db,self.project.name,relative,records,profile())
            for item in expired:
                previous_path=self.store.root/item['path']
                if previous_path.parent==folder and previous_path!=target:shutil.rmtree(previous_path,ignore_errors=True)
        except Exception:
            shutil.rmtree(target)
            raise

    def close(self):
        if self.closed:return
        if self.cancel_call:self.cancel_call.close()
        from .task_lifecycle import administrative,request_cleanup
        self.begin_cleanup()
        error=None
        try:
            # Failed/stale publication may have copied bytes but never committed
            # an artifact reference. Remove only files created by this runtime.
            from .task_leases import CURRENT
            token=CURRENT.set(None)
            try:
                rows=self.store.query('SELECT artifacts FROM agent_turns WHERE id=?',(self.lease.run_id,))
                referenced={a['file'] for a in json.loads(rows[0]['artifacts'])} if rows else set()
                for path in getattr(self,'exports',[]):
                    if path.name not in referenced:path.unlink(missing_ok=True)
            finally:CURRENT.reset(token)
        except Exception as exc:error=exc
        try:
            self.identity.close()
            from .image_sandbox import release_reclaimed_gpus
            release_reclaimed_gpus(self.identity.pool)
        except Exception as exc:error=exc
        self.closed=True
        with administrative(),self.store.connect() as db:
            db.execute('UPDATE execution_cleanup SET state=?,error=?,updated=? WHERE attempt=?',
                ('quarantined' if error else 'complete','资源回收未完成，已隔离等待回收器处理。' if error else '',self.store.now(),self.lease.attempt))
        if error:raise error
