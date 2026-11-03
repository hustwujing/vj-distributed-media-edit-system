"""Authenticated inherited-channel broker between unprivileged HTTP and launcher.

There is no arbitrary command/path operation. Every request is re-checked against
the parent's task lease and immutable asset scope before a fixed tool runs.
"""
from .deployment_identity import service_user, runtime_group, socket_root

import base64
import json
import os
from pathlib import Path
import pwd
import socket
import struct
import subprocess
import sys
import tempfile
import threading
import select
import stat
from pydantic import ValidationError
from .task_leases import TaskQuotaReached

MAX_FRAME=384*1024**2
FILE_PERMISSION_ERROR={'code':'filesystem_permission_denied',
                       'message':'内部文件权限异常，工具操作未完成，请联系管理员检查任务诊断。'}


def await_ready(process):
    """Fail startup promptly instead of leaving a socket with no reader."""
    readable,_,_=select.select([process.stdout],[],[],8)
    ready=bool(readable and os.read(process.stdout.fileno(),16)==b'READY\n')
    process.stdout.close()
    if not ready:
        if process.poll() is None:process.kill()
        process.wait(timeout=3)
        raise RuntimeError('Unprivileged task gateway could not start')


def receive(sock,limit=MAX_FRAME):
    def exact(size):
        chunks=[]
        while size:
            chunk=sock.recv(min(size,1024*1024))
            if not chunk:raise EOFError()
            chunks.append(chunk);size-=len(chunk)
        return b''.join(chunks)
    length=struct.unpack('!Q',exact(8))[0]
    if length>limit:raise ValueError('Broker frame exceeds limit')
    value=json.loads(exact(length))
    if not isinstance(value,dict):raise ValueError('Invalid broker frame')
    return value


def send(sock,value):
    body=json.dumps(value,ensure_ascii=False).encode()
    if len(body)>MAX_FRAME:raise ValueError('Broker response exceeds limit')
    sock.sendall(struct.pack('!Q',len(body))+body)


def grant_database(store):
    account=pwd.getpwnam(service_user())
    for directory in [store.root,*store.root.parents]:
        if directory==Path('/'):break
        if directory!=store.root and directory.stat().st_mode&0o001:break
        subprocess.run(['setfacl','-m',f'u:{account.pw_uid}:rwx' if directory==store.root else f'u:{account.pw_uid}:r-x',str(directory)],check=True,capture_output=True)
    subprocess.run(['setfacl','-m',f'd:u:{account.pw_uid}:rwx',str(store.root)],check=True,capture_output=True)
    for path in store.root.glob('state.sqlite3*'):
        if path.is_file() and not path.is_symlink():
            try:descriptor=os.open(path,os.O_RDONLY|os.O_NOFOLLOW)
            except FileNotFoundError:continue  # WAL was checkpointed and unlinked.
            try:
                subprocess.run(['setfacl','-m',f'u:{account.pw_uid}:rw',f'/proc/self/fd/{descriptor}'],
                    pass_fds=(descriptor,),check=True,capture_output=True)
            finally:os.close(descriptor)


class Client:
    def __init__(self,fd):self.socket=socket.socket(fileno=fd);self.lock=threading.Lock()
    def request(self,operation,body):
        with self.lock:
            send(self.socket,{'operation':operation,'body':body})
            response=receive(self.socket)
        if response['status']!=200:
            error=response.get('body',{}).get('error','Task tool failed')
            if isinstance(error,dict) and error.get('code')==FILE_PERMISSION_ERROR['code']:
                raise PermissionError(FILE_PERMISSION_ERROR['message'])
            if isinstance(error,dict) and error.get('type')=='quota':
                raise TaskQuotaReached(error.get('message','Task quota exhausted'),
                    **{key:error.get(key) for key in ('category','scope','used','limit','reset_at')})
            raise ValueError(error.get('message','Task tool failed') if isinstance(error,dict) else error)
        return response['body']
    def relay(self,operation,body,handler):
        try:handler.reply(200,self.request(operation,body))
        except TaskQuotaReached as exc:handler.reply(403,{'error':{'type':'quota','message':str(exc),**exc.diagnostic}})
        except PermissionError:handler.reply(500,{'error':FILE_PERMISSION_ERROR})
        except ValueError as exc:handler.reply(422,{'error':str(exc)})


class GatewayProcess:
    def __init__(self,gateway):
        self.gateway=gateway
        self.server,self.client=socket.socketpair()
        self.process=None
        self.thread=threading.Thread(target=self.serve,daemon=True)

    def serve(self):
        gateway=self.gateway
        while True:
            try:request=receive(self.server,32*1024**2)
            except (EOFError,OSError,ValueError,RecursionError):
                self.server.close();return
            try:
                with gateway.lease.bind():
                    gateway.check()
                    body=request['body'];operation=request['operation']
                    if not isinstance(body,dict):raise ValueError('Invalid tool request')
                    if operation=='workflow_preflight':
                        if gateway.mode!='execute' or not gateway.workflow_callback or getattr(gateway,'workflow_review_id',None):raise ValueError('预检查不属于本轮权限')
                        if body.get('action')!='preflight':raise ValueError('Invalid preflight action')
                        response={'status':200,'body':gateway.workflow_callback(body)}
                    elif operation=='normalize':
                        from .workflow_gateway import MODEL_PATHS
                        if gateway.allowed_paths is not None and not (gateway.allowed_paths&MODEL_PATHS):raise ValueError('Model input is outside task scope')
                        from .egress_policy import model_input
                        model_input(body);gateway.normalize_model_images(body)
                        response={'status':200,'body':body}
                    elif operation=='workflow_inspect':
                        from .workflow_gateway import inspect
                        response={'status':200,'body':inspect(gateway,body)}
                    elif operation=='decode':
                        raw=base64.b64decode(body['image'],validate=True)
                        if len(raw)>20*1024**2:raise ValueError('Image too large')
                        from .agent_sandbox import command
                        runtime=gateway.lease.runtime
                        with tempfile.TemporaryFile(dir=runtime.path/'data') as output,tempfile.TemporaryFile(dir=runtime.path/'data') as errors:
                            result=subprocess.run(command(runtime.env,None,['/opt/venv/bin/python','/opt/canvas_pixels.py']),
                                input=raw,stdout=output,stderr=errors,timeout=30)
                            if result.returncode:raise ValueError('Invalid image')
                            output.seek(0);data=output.read(128*1024**2)
                        response={'status':200,'body':{'image':base64.b64encode(data).decode()}}
                    elif operation=='extract':
                        if gateway.allowed_paths is not None and '/transcribe' not in gateway.allowed_paths:raise ValueError('Transcription is not authorized')
                        asset=gateway.assets.get(body.get('asset'));track=body.get('track',0)
                        if not asset or not isinstance(track,int) or not 0<=track<len(asset['metadata'].get('audio_tracks',[])):
                            raise ValueError('Invalid project audio asset')
                        with tempfile.TemporaryDirectory(dir=gateway.lease.runtime.path/'data') as folder:
                            path=Path(folder)/'audio.wav'
                            gateway.extract_audio(asset['filename'],track,path)
                            if path.stat().st_size>256*1024**2:raise ValueError('Audio too large')
                            response={'status':200,'body':{'audio':base64.b64encode(path.read_bytes()).decode()}}
                    elif operation=='refresh_transcript':
                        if gateway.allowed_paths is not None and '/transcribe' not in gateway.allowed_paths:raise ValueError('Transcription is not authorized')
                        asset=gateway.assets.get(body.get('asset'));track=body.get('track',0)
                        if not asset or not isinstance(track,int) or not 0<=track<len(asset['metadata'].get('audio_tracks',[])):
                            raise ValueError('Invalid project audio asset')
                        suffix=f'.track{track}' if track else ''
                        name=Path(asset['filename']).stem+suffix+'.json'
                        source=gateway.project_dir/'edit/transcripts'/name
                        if source.is_symlink():raise ValueError('Invalid transcript')
                        descriptor=os.open(source,os.O_RDONLY|os.O_NOFOLLOW|os.O_NONBLOCK)
                        try:
                            info=os.fstat(descriptor)
                            if not stat.S_ISREG(info.st_mode) or info.st_nlink!=1 or info.st_size>32*1024**2:raise ValueError('Invalid transcript')
                            # Cached downloads can be root-owned; newly generated
                            # files can be service-owned. Keep ownership intact.
                            os.fchmod(descriptor,0o640)
                        finally:os.close(descriptor)
                        if gateway.lease.store.shared_files:
                            gateway.lease.store.artifacts.save_file(gateway.project_dir.name,'edit/transcripts/'+name)
                        target=gateway.lease.runtime.transcripts/name
                        with target.open('w') as stream:stream.write(source.read_text())
                        os.chown(target,gateway.uid,gateway.gid);target.chmod(0o600)
                        response={'status':200,'body':{}}
                    elif operation=='image':
                        if gateway.allowed_paths is not None and '/image-tools' not in gateway.allowed_paths:raise ValueError('Image tools are not authorized')
                        class Reply:
                            value=None
                            def reply(self,status,payload):self.value={'status':status,'body':payload}
                        reply=Reply();gateway.image_edit(reply,body)
                        response=reply.value
                    else:raise ValueError('Unknown privileged operation')
            except TaskQuotaReached as exc:
                from .task_leases import record_quota
                record_quota(gateway.lease.store,gateway.lease.run_id,exc)
                from .telemetry import emit
                emit('gateway.quota_exhausted','WARNING',exc=exc,task_id=gateway.lease.run_id,attempt_id=gateway.lease.attempt,service='gateway')
                response={'status':403,'body':{'error':{'type':'quota','message':str(exc),**exc.diagnostic}}}
            except ValidationError as exc:
                from .telemetry import emit
                emit('gateway.validation_failed','WARNING',exc=exc,task_id=gateway.lease.run_id,attempt_id=gateway.lease.attempt,service='gateway')
                # Do not echo input values, custom validator messages or context:
                # these can contain credentials from a downstream adapter.
                fields=['.'.join(str(part)[:80] for part in item['loc'])+': '+item['type']
                        for item in exc.errors(include_input=False,include_context=False,include_url=False)[:20]]
                response={'status':422,'body':{'error':{'code':'request_validation_failed',
                    'message':'请求参数校验未通过，请修正字段后重试：'+'; '.join(fields)}}}
            except PermissionError as exc:
                from .telemetry import emit
                emit('gateway.filesystem_permission_failed','ERROR',exc=exc,task_id=gateway.lease.run_id,attempt_id=gateway.lease.attempt,service='gateway')
                response={'status':500,'body':{'error':FILE_PERMISSION_ERROR}}
            except ValueError as exc:
                from .telemetry import emit
                emit('gateway.validation_failed','WARNING',exc=exc,task_id=gateway.lease.run_id,attempt_id=gateway.lease.attempt,service='gateway')
                response={'status':422,'body':{'error':{'code':'request_validation_failed',
                    'message':'请求参数或执行条件未通过校验，请检查任务诊断。'}}}
            except Exception as exc:
                from .telemetry import emit
                emit('gateway.operation_failed','ERROR',exc=exc,task_id=gateway.lease.run_id,attempt_id=gateway.lease.attempt,service='gateway')
                response={'status':422,'body':{'error':'工具操作未完成，请检查任务诊断。'}}
            try:send(self.server,response)
            except (OSError,ValueError):return

    def start(self):
        gateway=self.gateway;lease=gateway.lease
        grant_database(lease.store)
        account=pwd.getpwnam(service_user())
        gateway.project_dir.parent.mkdir(parents=True,exist_ok=True)
        subprocess.run(['setfacl','-m',f'u:{account.pw_uid}:--x',str(gateway.project_dir.parent)],check=True,capture_output=True)
        for path in (gateway.project_dir,gateway.project_dir/'edit',gateway.project_dir/'edit/transcripts',gateway.project_dir/'sources'):
            if path.is_symlink():raise ValueError('Invalid broker project directory')
            path.mkdir(mode=0o700,parents=True,exist_ok=True)
            subprocess.run(['setfacl','-m',f'u:{account.pw_uid}:rwx',str(path)],check=True,capture_output=True)
        for path in (gateway.project_dir/'edit/transcripts').glob('*.json'):
            if path.is_file() and not path.is_symlink():subprocess.run(['setfacl','-m',f'u:{account.pw_uid}:rw',str(path)],check=True,capture_output=True)
        runtime=lease.runtime
        base=socket_root()/'client-temp';base.mkdir(mode=0o700,parents=True,exist_ok=True)
        for path in (base.parent,base):subprocess.run(['setfacl','-m',f'u:{account.pw_uid}:--x',str(path)],check=True,capture_output=True)
        private=base/lease.attempt;private.mkdir(mode=0o700)
        subprocess.run(['mount','-t','tmpfs','-o',f'size=536870912,nr_inodes=1024,mode=0700,uid={account.pw_uid},gid={account.pw_gid},nosuid,nodev,noexec','tmpfs',str(private)],check=True,capture_output=True)
        runtime.identity.save(gateway_temp=str(private))
        config={'root':str(lease.store.root),'project':str(gateway.project_dir),'assets':list(gateway.assets.values()),
                'storage_credentials':{k:os.environ[k] for k in ('AWS_ACCESS_KEY_ID','AWS_SECRET_ACCESS_KEY','AWS_SESSION_TOKEN','AWS_DEFAULT_REGION','VIDEO_USE_OBJECT_ENCRYPTION_KEY') if k in os.environ} if lease.store.shared_files else {},
                'mode':gateway.mode,'model':gateway.model,'base_url':gateway.base_url,'key':gateway.key,
                'agent_profile':gateway.profile.snapshot(),
                'image_video_key':os.environ.get('VIDEO_USE_IMAGE_VIDEO_KEY',''),'generation_key':os.environ.get('VIDEO_USE_GENERATION_KEY',''),'image_generation_key':os.environ.get('VIDEO_USE_IMAGE_GENERATION_KEY',''),'eleven_key':os.environ.get('ELEVENLABS_API_KEY',''),'uid':gateway.uid,'gid':gateway.gid,
                'socket':str(gateway.socket_path),'workflow':bool(gateway.workflow_callback),'canvas':bool(gateway.canvas_callback),
                'workflow_review_id':getattr(gateway,'workflow_review_id',None),
                'parent':os.getpid(),
                'lease':{key:getattr(lease,key) for key in ('run_id','worker','attempt','generation','kind','recovered','prior_kind','ttl','step_id')}}
        env={'PATH':'/usr/bin:/bin','PYTHONPATH':str(Path(__file__).resolve().parents[1]),'PYTHONDONTWRITEBYTECODE':'1','TMPDIR':str(private)}
        env.update({k:v for k,v in os.environ.items() if k.startswith('VIDEO_USE_') and not any(s in k for s in ('KEY','TOKEN','SECRET','PASSWORD'))})
        env['VIDEO_USE_TELEMETRY_STREAM']='stderr'
        env['VIDEO_USE_SERVICE']='gateway'
        fds=(gateway.server.fileno(),self.client.fileno())
        self.process=subprocess.Popen(['/usr/bin/setpriv',f'--reuid={account.pw_uid}',f'--regid={account.pw_gid}',
            '--clear-groups','--no-new-privs','--bounding-set=-all',sys.executable,'-m','webapp.gateway_service',*map(str,fds)],
            stdin=subprocess.PIPE,stdout=subprocess.PIPE,stderr=None,env=env,pass_fds=fds)
        self.process.stdin.write(json.dumps(config).encode());self.process.stdin.close()
        try:
            start=Path(f'/proc/{self.process.pid}/stat').read_text().rsplit(')',1)[1].split()[19]
            runtime.identity.save(guard_processes=[{'pid':self.process.pid,'start':start}])
        except FileNotFoundError:pass
        self.client.close();await_ready(self.process);self.thread.start()

    def close(self):
        if self.process and self.process.poll() is None:
            self.process.terminate()
            try:self.process.wait(timeout=3)
            except subprocess.TimeoutExpired:self.process.kill();self.process.wait(timeout=3)
        self.server.close()
        if self.thread.is_alive():self.thread.join(timeout=1)
