"""OpenSandbox runtime for fixed, CPU-only operation protocol commands."""
from __future__ import annotations

from datetime import timedelta
import concurrent.futures
from contextlib import closing
import fcntl
import hashlib
import json
import os
import re
from pathlib import Path,PurePosixPath
import shutil
import stat
import threading
import time
import uuid

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
        self.local_lock=None
        self.limit=int(os.environ.get('VIDEO_USE_TASK_DISK_BYTES',str(2*1024**3)))
        self.max_output_files=50000

    def revoked(self):return False
    def pulse(self,seconds):pass
    def begin_cleanup(self):pass
    def quiesce(self):pass
    def checkpoint(self):
        if self.env is None:return None
        from .step_checkpoints import save
        return save(self)
    def scope(self):return {'paths':[],'stopped':False}
    def close_scope(self,scope):pass

    def environment(self,store,w,model):
        if self.env is not None:return self.env
        project=w['project'];step=next(item for item in w['steps'] if item['id']==self.lease.step_id)
        root=store.root/'command-attempts'/self.attempt
        if root.exists():raise RuntimeError('Command attempt workspace already exists')
        workspace=root/'workspace';sources=root/'sources'
        root.mkdir(parents=True,mode=0o700)
        self.local_root=root
        self.local_lock=os.open(root/'.owner',os.O_CREAT|os.O_EXCL|os.O_RDWR|os.O_NOFOLLOW,0o600)
        fcntl.flock(self.local_lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
        workspace.mkdir(mode=0o700);sources.mkdir(mode=0o700)
        self.store=store;self.workspace=workspace;self.project=store.project_dir(project)
        self.project.mkdir(parents=True,exist_ok=True,mode=0o700)
        self.uid=self.service_uid=os.getuid();self.gid=self.service_gid=os.getgid()

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
        self.repair_input_paths=set()
        for reference in step['spec'].get('inputs',{}).values():
            if reference.get('repair_candidate'):
                if not w.get('dynamic') or step.get('purpose') not in ('repair','evidence'):
                    raise ValueError('Repair candidate outside scoped task')
                from .workflow_graph import get as graph_get
                from .repair_inputs import lookup
                candidate=lookup(store,graph_get(store,w['id']),reference['repair_candidate'])
                restore(candidate['record'],destination(workspace,candidate['path']))
                self.repair_input_paths.add(candidate['path'])
            elif reference.get('asset'):
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

    def _run(self,command,timeout,cancelled):
        if cancelled():raise RuntimeError('Command operation cancelled')
        try:
            from opensandbox.models.execd import RunCommandOpts
            from opensandbox.models.execd_sync import ExecutionHandlersSync
            opts=RunCommandOpts(working_directory='/workspace',timeout=timedelta(seconds=timeout),
                uid=10001,gid=10001,envs={'PATH':'/opt/venv/bin:/usr/bin:/bin',
                    'PYTHONPATH':'/opt/venv/lib/python3.13/site-packages/webapp:/opt/video-use/helpers',
                    'HOME':'/state','LANG':'C.UTF-8'})
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
            diagnostic={'code':'opensandbox_command_failed','exit_code':execution.exit_code,'log_tail':detail}
            message=detail;receipt=None
            for line in reversed(self._stdout(execution)[-32768:].splitlines()):
                try:value=json.loads(line)
                except (ValueError,RecursionError):continue
                if isinstance(value,dict) and isinstance(value.get('message'),str) and ('log_tail' in value or 'exit_code' in value):receipt=value;break
            if receipt is not None:
                message=receipt['message'];tail=receipt.get('log_tail')
                if isinstance(tail,str):diagnostic['log_tail']=tail[-4000:]
                if receipt.get('code') in ('output_contract','preflight','operation_input_contract','parameters'):
                    diagnostic['code']=receipt['code']
                for key in ('output','report'):
                    if isinstance(receipt.get(key),dict):diagnostic[key]=receipt[key]
                error_type=receipt.get('error_type')
                if isinstance(error_type,str) and error_type.isidentifier() and len(error_type)<=80:diagnostic['error_type']=error_type
                # Keep the child error as data. Do not embed an escaped JSON
                # receipt in str(exc), where the public message is truncated.
            for line in diagnostic['log_tail'].splitlines():
                match=re.match(r'^(?:[A-Za-z_]\w*\.)*([A-Za-z_]\w*(?:Error|Exception)):\s*(.+)$',line)
                if match:diagnostic['error_type'],message=match.groups()
            error=RuntimeError('OpenSandbox command operation failed'+(': '+message[:2000] if message else ''))
            error.diagnostic=diagnostic
            raise error
        return execution

    def execute_payload(self,env,payload,timeout,cancelled):
        self._upload_tree(env);request='/run/video-use/operation-request.json'
        self.session.files.write(request,json.dumps(payload,ensure_ascii=False).encode(),0o600)
        module='webapp.operation_io' if payload.get('mode') in ('prepare_image','composite_image','fit_image') else 'webapp.workflow_io'
        execution=self._run(['/bin/sh','-c',f'exec /opt/venv/bin/python -m {module} < {request}'],timeout,cancelled)
        output=payload.get('output')
        if output:self._download_output(env,output)
        if payload.get('mode')=='local':return json.loads(self._stdout(execution))

    def validate_payload(self,payload,timeout=90):
        request='/run/video-use/quality-request.json'
        self.session.files.write(request,json.dumps(payload,ensure_ascii=False).encode(),0o600)
        execution=self._run(['/bin/sh','-c',f'exec /opt/venv/bin/python -m webapp.quality_check < {request}'],timeout,lambda:False)
        return json.loads(self._stdout(execution))

    def _download_output(self,env,relative):
        path=PurePosixPath(relative)
        if path.is_absolute() or '..' in path.parts or not path.parts:raise ValueError('Invalid command output path')
        output=PurePosixPath('/workspace')/path
        total=count=0;seen=set()
        for entry in self.session.files.list(str(output),depth=100):
            self.lease.check();count+=1
            if count>self.max_output_files:raise ValueError('Command output exceeds file limit')
            remote=PurePosixPath(entry['path'] if isinstance(entry,dict) else entry.path)
            kind=entry.get('type') if isinstance(entry,dict) else entry.entry_type
            if '..' in remote.parts:raise ValueError('Command output escaped workspace')
            try:
                remote.relative_to(output)
                name=remote.relative_to('/workspace')
            except ValueError:raise ValueError('Command output escaped workspace') from None
            if kind in ('directory','dir'):continue
            if kind not in ('file','regular') or not name.parts:raise ValueError('Command output is not a regular file')
            if name in seen:raise ValueError('Duplicate command output path')
            seen.add(name)
            # Directory FDs and O_NOFOLLOW protect every local path component.
            # Replacing a private temporary file never truncates an existing
            # symlink/hardlink target, even if an entry changes during download.
            parent=os.open(env['workspace'],os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW)
            temporary=None
            try:
                for part in name.parts[:-1]:
                    try:os.mkdir(part,0o700,dir_fd=parent)
                    except FileExistsError:pass
                    child=os.open(part,os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW,dir_fd=parent)
                    os.close(parent);parent=child
                try:info=os.stat(name.name,dir_fd=parent,follow_symlinks=False)
                except FileNotFoundError:info=None
                if info and (not stat.S_ISREG(info.st_mode) or info.st_nlink!=1):
                    raise ValueError('Unsafe local command output target')
                temporary='.'+uuid.uuid4().hex+'.pending'
                fd=os.open(temporary,os.O_WRONLY|os.O_CREAT|os.O_EXCL|os.O_NOFOLLOW,0o600,dir_fd=parent)
                with os.fdopen(fd,'wb') as stream,closing(self.session.files.read_chunks(str(remote))) as chunks:
                    for chunk in chunks:
                        self.lease.check()
                        if not isinstance(chunk,bytes):raise ValueError('Invalid command output bytes')
                        if total+len(chunk)>self.limit:raise ValueError('Command output exceeds byte limit')
                        stream.write(chunk);total+=len(chunk)
                os.replace(temporary,name.name,src_dir_fd=parent,dst_dir_fd=parent);temporary=None
            finally:
                if temporary is not None:os.unlink(temporary,dir_fd=parent)
                os.close(parent)

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
            if self.local_lock is not None:
                os.close(self.local_lock);self.local_lock=None
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
