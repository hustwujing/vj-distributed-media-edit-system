"""Native Harness runtime with the existing Task-scoped workspace/checkpoints."""
from datetime import timedelta
import hashlib
import json
import os
from pathlib import Path
import time
import uuid

from .opensandbox_command import OpenSandboxCommandRuntime,OpenSandboxCommandBackend,OpenSandboxCommandFactory
from .runtime_client import RuntimeClient,RuntimeTransportError
from .runtime_injection import RuntimeBundle,RuntimeInjector
from .runtime_protocol import RuntimeIdentity


class NativeImplementerRuntime(OpenSandboxCommandRuntime):
    is_native=True

    def __init__(self,*args,**kwargs):
        super().__init__(*args,**kwargs)
        self.gateway=None;self.client=None;self.frozen=False

    def normalize_image(self,encoded):
        name='/run/video-use/image-'+uuid.uuid4().hex+'.json'
        self.session.files.write(name,json.dumps({'data':encoded}).encode(),0o600)
        try:
            result=self._run(['/bin/sh','-c',f'/opt/venv/bin/python -m webapp.native_model_image < {name}'],
                30,lambda:self.lease.lost.is_set())
            value=json.loads(self._stdout(result))
            if not isinstance(value.get('data'),str) or len(value['data'])>3*1024**2:
                raise ValueError('Invalid normalized image receipt')
            return value['data']
        finally:self.session.files.delete(name,missing_ok=True)

    def start_harness(self,profile,binding,on_event,*,restore=None):
        from .native_gateway import NativeAttemptGateway
        from .runtime_bootstrap import CONTROL_PORT
        from opensandbox.models.execd import RunCommandOpts
        identity=RuntimeIdentity(self.attempt,self.lease.generation,self.sandbox_id)
        from .native_network import verify
        verify(os.environ['VIDEO_USE_NODE_GATEWAY_BIND'],os.environ['VIDEO_USE_NODE_GATEWAY_SUBNET'])
        # Read the sandbox's actual bridge source before starting untrusted code.
        source=self._run(['/opt/venv/bin/python','-c',
            'import socket; print(socket.gethostbyname(socket.gethostname()))'],10,lambda:False)
        source_ip=self._stdout(source).strip()
        self.gateway=NativeAttemptGateway(self.lease,identity,profile,source_ip,self.normalize_image)
        tools=Path(__file__).resolve().parents[1]/'runtime/public/capabilities.json'
        frozen={'harness':profile.harness,'runtime_profile':binding.runtime_profile,
            'image_digest':binding.image_digest,'skill_digest':binding.skill_digest,
            'tool_schema_digest':'sha256:'+hashlib.sha256(tools.read_bytes()).hexdigest(),
            'gateway_protocol':'gateway-v1'}
        bundle=RuntimeBundle.create(identity,profile.harness,
            {'model':profile.model,'reasoning_effort':profile.reasoning_effort},frozen,
            {'model':profile.model,'reasoning_effort':profile.reasoning_effort,
             'max_output_tokens':profile.budget['max_output_tokens']},gateway=self.gateway.material)
        def workspace(files):
            self._upload_tree(self.env)
            if restore:
                from .native_sessions import restore as restore_session
                restore_session(self,restore,files)
        RuntimeInjector(self.session.files).inject(bundle,workspace)
        opts=RunCommandOpts(background=True,uid=10001,gid=10001,working_directory='/workspace',
            timeout=timedelta(seconds=min(profile.budget['run_seconds'],3600)+60),
            envs={'PATH':'/opt/venv/bin:/usr/bin:/bin','HOME':'/state','TMPDIR':'/state/tmp','LANG':'C.UTF-8'})
        self.session.commands.run(['/opt/venv/bin/python','-m','webapp.runtime_bootstrap'],opts=opts)
        endpoint,headers=self.session.websocket_endpoint(CONTROL_PORT)
        self.client=RuntimeClient(endpoint,identity,bundle.control_token,on_event,headers=headers,timeout=2)
        deadline=time.monotonic()+30
        while True:
            self.lease.check()
            try:self.client.connect();break
            except (RuntimeTransportError,TimeoutError):
                if time.monotonic()>=deadline:raise
                time.sleep(.2)
        self.client.timeout=30;self.client.call('runtime.initialize',frozen)
        self.client.timeout=2

    def export_native_session(self):
        if not self.frozen:raise ValueError('Native children must stop before session export')
        result=self.client.call('session.export',{})
        reference=result['manifest_ref']
        if reference['path']!='/run/video-use/export-manifest.json' or not 0<reference['size']<=8*1024**2:
            raise ValueError('Invalid native export descriptor')
        data=self.session.files.read(reference['path'])
        if len(data)!=reference['size'] or hashlib.sha256(data).hexdigest()!=reference['sha256']:
            raise ValueError('Native export manifest integrity mismatch')
        return json.loads(data)

    def freeze_harness(self):
        if self.client and not self.frozen:
            self.client.timeout=20
            self.client.call('session.freeze',{})
            self.frozen=True

    def begin_cleanup(self):
        if self.gateway:self.gateway.revoke()

    def quiesce(self):
        # Completed native children must be reaped before checkpoints/validation.
        # During cancellation the host closes the transport and deletes the
        # entire sandbox, including detached native children.
        if self.client and not self.lease.lost.is_set():self.freeze_harness()

    def close(self):
        self.begin_cleanup()
        try:
            if self.client:self.client.close()
            super().close()
        finally:
            if self.gateway:self.gateway.close()


def factory_from_environment(environ=None):
    env=os.environ if environ is None else environ
    if env.get('VIDEO_USE_CPU_AGENT_SANDBOX_BACKEND',env.get('VIDEO_USE_CPU_SANDBOX_BACKEND','local_bwrap'))!='opensandbox':return None
    from .opensandbox_sdk import OpenSandboxSDK
    from .sandbox_backends import OpenSandboxClient,RequestsTransport,BackendConfigurationError
    base=env.get('VIDEO_USE_OPEN_SANDBOX_URL','');key=env.get('VIDEO_USE_OPEN_SANDBOX_API_KEY','')
    if not base or not key:raise BackendConfigurationError('OpenSandbox URL and API key are required')
    if env.get('VIDEO_USE_OPEN_SANDBOX_NETWORK_ISOLATION')!='internal_bridge' or env.get('VIDEO_USE_OPEN_SANDBOX_INTERNAL_BRIDGE_CONFIRMED')!='1':
        raise BackendConfigurationError('Native Harness requires the confirmed internal sandbox bridge')
    required=('VIDEO_USE_NODE_GATEWAY_BIND','VIDEO_USE_NODE_GATEWAY_SUBNET',
              'VIDEO_USE_NODE_GATEWAY_CERT','VIDEO_USE_NODE_GATEWAY_KEY','VIDEO_USE_NODE_GATEWAY_CA',
              'VIDEO_USE_OPEN_SANDBOX_AGENT_PROFILES_JSON')
    if any(not env.get(name) for name in required):raise BackendConfigurationError('Native Harness Gateway/Runtime configuration is incomplete')
    lifecycle=OpenSandboxClient(RequestsTransport(base,key,timeout=30),network_policy='internal_bridge')
    sdk=OpenSandboxSDK(base,key);sdk.preflight()
    return OpenSandboxCommandFactory(OpenSandboxCommandBackend(lifecycle,sdk,runtime_type=NativeImplementerRuntime))
