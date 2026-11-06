"""OpenSandbox runtime for fixed, CPU-only operation protocol commands."""
from __future__ import annotations

from datetime import timedelta
import concurrent.futures
import hashlib
import json
import os
from pathlib import Path,PurePosixPath
import shutil
import threading
import time

from .sandbox_backends import OPEN_SANDBOX,BackendConfigurationError,OpenSandboxCleanupRegistry


class OpenSandboxCommandRuntime:
    is_command=True
    is_remote=False
    def __init__(self,lifecycle,session,sandbox_id,attempt,lease,cleanup_registry=None):
        self.lifecycle=lifecycle;self.session=session;self.sandbox_id=sandbox_id
        self.attempt=attempt;self.lease=lease;self.env=None;self.closed=False
        self.session_closed=False;self.sandbox_deleted=False;self.cleanup_lock=threading.RLock()
        self.cleanup_registry=cleanup_registry;self.registry_completed=cleanup_registry is None
        self.local_root=None

    def revoked(self):return False
    def pulse(self,seconds):pass
    def begin_cleanup(self):pass
    def quiesce(self):pass
    def checkpoint(self):return None
    def scope(self):return {'paths':[],'stopped':False}
    def close_scope(self,scope):pass

    def environment(self,store,w,model):
        if self.env is not None:return self.env
        project=w['project'];step=next(item for item in w['steps'] if item['id']==self.lease.step_id)
        root=store.root/'command-attempts'/self.attempt
        if root.exists():raise RuntimeError('Command attempt workspace already exists')
        workspace=root/'workspace';sources=root/'sources'
        workspace.mkdir(parents=True,mode=0o700);sources.mkdir(mode=0o700)
        self.local_root=root

        def destination(base,relative):
            relative=PurePosixPath(relative)
            if relative.is_absolute() or '..' in relative.parts:raise ValueError('Command input escaped workspace')
            target=base/Path(*relative.parts);target.parent.mkdir(parents=True,exist_ok=True,mode=0o700)
            return target

        def restore(record,target):
            if store.shared_files:store.artifacts.materialize(project,record['artifact'],target)
            else:shutil.copyfile(store.root/record['location'],target)
            with target.open('rb') as stream:
                if target.stat().st_size!=record['size'] or hashlib.file_digest(stream,'sha256').hexdigest()!=record['sha256']:
                    raise ValueError('Command input integrity mismatch')
            target.chmod(0o600)

        assets={asset['id']:asset for asset in store.assets(project)};selected=[]
        for reference in step['spec'].get('inputs',{}).values():
            if reference.get('asset'):
                asset=assets.get(reference['asset'])
                if not asset:raise ValueError('Command input asset is outside project')
                logical='sources/'+asset['filename'];target=destination(sources,asset['filename'])
                if store.shared_files:
                    record=store.artifacts.resolve(project,logical)
                    store.artifacts.materialize(project,record['id'],target)
                else:shutil.copyfile(store.project_dir(project)/logical,target)
                target.chmod(0o600)
                selected.append(asset)
            elif reference.get('step'):
                parent=next((item for item in w['steps'] if item['id']==reference['step']),None)
                if not parent or parent['status']!='succeeded':raise ValueError('Command dependency is not accepted')
                relative=(parent['quality'].get('output_roles') or {}).get(reference['role'])
                rows=store.query('SELECT manifest FROM execution_step_checkpoints WHERE workflow=? AND step=? AND revision=? AND project=?',
                    (w['id'],parent['id'],parent['revision'],project))
                record=json.loads(rows[0]['manifest']).get(relative) if rows and relative else None
                if not record:raise ValueError('Command dependency has no durable artifact')
                restore(record,destination(workspace,relative))

        allowed=set(step['spec'].get('preflight',[])+step['spec'].get('config_files',[]))
        allowed.update(value for value in step['spec'].get('argv',[]) if isinstance(value,str) and value.startswith('edit/'))
        checkpoints=store.query('SELECT manifest,path FROM execution_checkpoints WHERE project=?',(project,))
        if allowed:
            manifest=json.loads(checkpoints[0]['manifest']).get('workspace',{}) if checkpoints else {}
            for relative in allowed:
                record=manifest.get(relative)
                if not record:raise ValueError('Command script is absent from the frozen workspace')
                restore(record,destination(workspace,relative))
        self.env={'workspace':workspace,'sources':sources,'assets':selected,
                  'uid':os.getuid(),'gid':os.getgid(),'project':root,'runtime':self,
                  'mode':'plan','step_output':None}
        return self.env

    def _upload_tree(self,env):
        files=self.session.files
        for root,prefix in ((env['workspace'],'/workspace'),(env['sources'],'/workspace/sources')):
            for path in Path(root).rglob('*'):
                if path.is_symlink():raise ValueError('Command workspace contains a link')
                relative=path.relative_to(root).as_posix();target=prefix+'/'+relative
                if path.is_dir():files.mkdir(target,0o700)
                elif path.is_file():
                    with path.open('rb') as stream:files.write(target,stream,0o700 if path.stat().st_mode&0o100 else 0o600)

    @staticmethod
    def _stdout(execution):return ''.join(item.text for item in execution.logs.stdout)

    @staticmethod
    def _diagnostic(execution):
        streams=[]
        for name in ('stderr','stdout'):
            streams.extend(getattr(execution.logs,name,[]) or [])
        return ''.join(getattr(item,'text','') for item in streams)[-4000:]

    def execute_payload(self,env,payload,timeout,cancelled):
        if cancelled():raise RuntimeError('Command operation cancelled')
        self._upload_tree(env);request='/run/video-use/operation-request.json'
        self.session.files.write(request,json.dumps(payload,ensure_ascii=False).encode(),0o600)
        module='webapp.operation_io' if payload.get('mode') in ('prepare_image','composite_image','fit_image') else 'webapp.workflow_io'
        command=['/bin/sh','-c',f'exec /opt/venv/bin/python -m {module} < {request}']
        try:
            from opensandbox.models.execd import RunCommandOpts
            from opensandbox.models.execd_sync import ExecutionHandlersSync
            opts=RunCommandOpts(working_directory='/workspace',timeout=timedelta(seconds=timeout),
                uid=10001,gid=10001,envs={'PATH':'/opt/venv/bin:/usr/bin:/bin','HOME':'/state','LANG':'C.UTF-8'})
            execution_id=[None]
            handlers=ExecutionHandlersSync(on_init=lambda event:execution_id.__setitem__(0,event.id))
        except ImportError:
            opts={'working_directory':'/workspace','timeout':timeout,'uid':10001,'gid':10001}
            handlers=None;execution_id=[None]
        pool=concurrent.futures.ThreadPoolExecutor(max_workers=1)
        try:
            future=pool.submit(self.session.commands.run,command,opts=opts,handlers=handlers)
            deadline=time.monotonic()+timeout
            while not future.done():
                self.lease.check()
                if cancelled() or time.monotonic()>deadline:
                    if execution_id[0]:self.session.commands.interrupt(execution_id[0])
                    raise RuntimeError('OpenSandbox command operation cancelled or timed out')
                threading.Event().wait(.1)
            execution=future.result()
        finally:pool.shutdown(wait=False,cancel_futures=True)
        if execution.exit_code not in (None,0):
            detail=self._diagnostic(execution).strip()
            raise RuntimeError('OpenSandbox command operation failed'+(': '+detail if detail else ''))
        output=payload.get('output')
        if output:self._download_output(env,output)
        if payload.get('mode')=='local':return json.loads(self._stdout(execution))

    def _download_output(self,env,relative):
        path=PurePosixPath(relative)
        if path.is_absolute() or '..' in path.parts:raise ValueError('Invalid command output path')
        for entry in self.session.files.list('/workspace/'+path.as_posix(),depth=100):
            remote=PurePosixPath(entry['path'] if isinstance(entry,dict) else entry.path)
            kind=entry.get('type') if isinstance(entry,dict) else entry.entry_type
            if kind not in ('file','regular'):continue
            try:name=remote.relative_to('/workspace')
            except ValueError:raise ValueError('Command output escaped workspace') from None
            target=env['workspace']/name;target.parent.mkdir(parents=True,exist_ok=True,mode=0o700)
            target.write_bytes(self.session.files.read(str(remote)));target.chmod(0o600)

    def close(self):
        with self.cleanup_lock:
            errors=[]
            if not self.session_closed:
                try:self.session.close()
                except Exception as exc:errors.append(exc)
                else:self.session_closed=True
            if not self.sandbox_deleted:
                try:self.lifecycle.delete_confirmed(self.sandbox_id,expected_attempt=self.attempt)
                except Exception as exc:errors.append(exc)
                else:self.sandbox_deleted=True
            if self.sandbox_deleted and not self.registry_completed:
                try:self.cleanup_registry.complete(self.lease.store,self.attempt)
                except Exception as exc:errors.append(exc)
                else:self.registry_completed=True
            if self.local_root:shutil.rmtree(self.local_root,ignore_errors=True)
            self.closed=self.session_closed and self.sandbox_deleted and self.registry_completed
            if errors:raise errors[0]


class OpenSandboxCommandBackend:
    def __init__(self,lifecycle,sdk,cleanup_registry=None):
        self.lifecycle=lifecycle;self.sdk=sdk;self.cleanup_registry=cleanup_registry or OpenSandboxCleanupRegistry()
    def start(self,lease,binding,resources,*,timeout=3600):
        if binding.backend!=OPEN_SANDBOX or binding.runtime_kind!='command_runtime':
            raise BackendConfigurationError('Command backend requires command_runtime binding')
        lease.start_compute()
        self.cleanup_registry.reserve(lease)
        payload=self.lifecycle.create(lease,binding,resources,timeout=timeout,entrypoint=['tail','-f','/dev/null'])
        sandbox_id=(payload or {}).get('id')
        if not sandbox_id:raise RuntimeError('OpenSandbox command create returned no id')
        self.cleanup_registry.attach(lease,sandbox_id)
        try:session=self.sdk.connect(sandbox_id)
        except Exception:
            self.lifecycle.delete_confirmed(sandbox_id,expected_attempt=lease.attempt)
            self.cleanup_registry.complete(lease.store,lease.attempt)
            raise
        return OpenSandboxCommandRuntime(self.lifecycle,session,sandbox_id,lease.attempt,lease,self.cleanup_registry)


class OpenSandboxCommandFactory:
    def __init__(self,backend):self.backend=backend
    def __call__(self,lease,binding):
        from .scheduling import requirements
        return self.backend.start(lease,binding,
            requirements(lease.store,lease.run_id,lease.kind,step_id=lease.step_id))
