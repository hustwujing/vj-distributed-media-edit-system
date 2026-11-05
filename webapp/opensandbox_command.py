"""OpenSandbox runtime for fixed, CPU-only operation protocol commands."""
from __future__ import annotations

from datetime import timedelta
import concurrent.futures
import json
from pathlib import Path,PurePosixPath
import threading
import time

from .sandbox_backends import OPEN_SANDBOX,BackendConfigurationError


class OpenSandboxCommandRuntime:
    is_command=True
    is_remote=False
    def __init__(self,lifecycle,session,sandbox_id,attempt,lease):
        self.lifecycle=lifecycle;self.session=session;self.sandbox_id=sandbox_id
        self.attempt=attempt;self.lease=lease;self.env=None;self.closed=False

    def revoked(self):return False
    def pulse(self,seconds):pass
    def begin_cleanup(self):pass
    def quiesce(self):pass
    def checkpoint(self):return None
    def scope(self):return {'paths':[],'stopped':False}
    def close_scope(self,scope):pass

    def environment(self,store,w,model):
        if self.env is not None:return self.env
        from . import agent_sandbox
        current=self.lease.runtime;self.lease.runtime=None
        try:self.env=agent_sandbox.prepare(store,w['project'],model,'execute',w['run_id'])
        finally:self.lease.runtime=current
        self.env['runtime']=self
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
        if execution.exit_code not in (None,0):raise RuntimeError('OpenSandbox command operation failed')
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
        if self.closed:return
        self.closed=True;self.session.close()
        self.lifecycle.delete_confirmed(self.sandbox_id,expected_attempt=self.attempt)


class OpenSandboxCommandBackend:
    def __init__(self,lifecycle,sdk):self.lifecycle=lifecycle;self.sdk=sdk
    def start(self,lease,binding,resources,*,timeout=3600):
        if binding.backend!=OPEN_SANDBOX or binding.runtime_kind!='command_runtime':
            raise BackendConfigurationError('Command backend requires command_runtime binding')
        lease.start_compute()
        payload=self.lifecycle.create(lease,binding,resources,timeout=timeout,entrypoint=['tail','-f','/dev/null'])
        sandbox_id=(payload or {}).get('id')
        if not sandbox_id:raise RuntimeError('OpenSandbox command create returned no id')
        try:session=self.sdk.connect(sandbox_id)
        except Exception:
            self.lifecycle.delete_confirmed(sandbox_id,expected_attempt=lease.attempt);raise
        return OpenSandboxCommandRuntime(self.lifecycle,session,sandbox_id,lease.attempt,lease)


class OpenSandboxCommandFactory:
    def __init__(self,backend):self.backend=backend
    def __call__(self,lease,binding):
        from .scheduling import requirements
        return self.backend.start(lease,binding,
            requirements(lease.store,lease.run_id,lease.kind,step_id=lease.step_id))
