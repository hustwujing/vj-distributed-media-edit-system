"""OpenSandbox Agent runtime orchestration without business-state ownership."""
from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import threading

from .runtime_bootstrap import CONTROL_PORT
from .runtime_client import RuntimeClient
from .runtime_injection import GatewayGrant,RuntimeBundle,RuntimeInjector
from .runtime_protocol import RuntimeIdentity
from .sandbox_backends import OPEN_SANDBOX,BackendConfigurationError


BOOTSTRAP_ENTRYPOINT=['/opt/venv/bin/python','-m','webapp.runtime_bootstrap',
                      '--config','/run/video-use/runtime.json']


class OpenSandboxProvisioningError(RuntimeError):
    def __init__(self,cause,cleanup_error=None):
        self.cause=cause;self.cleanup_error=cleanup_error
        detail='; cleanup also failed: '+type(cleanup_error).__name__ if cleanup_error else ''
        super().__init__('OpenSandbox runtime provisioning failed: '+type(cause).__name__+detail)


@dataclass(frozen=True)
class AgentRuntimeSpec:
    harness: str
    model: str
    reasoning_effort: str
    response_transport: str
    tool_schema_digest: str
    gateway_protocol: str
    harness_config: dict
    gateway_grant_factory: object = None


@dataclass(frozen=True)
class RuntimeGatewayGrant:
    material: GatewayGrant
    revoke: object


class OpenSandboxRuntime:
    def __init__(self,lifecycle,session,client,sandbox_id,attempt,revoke_gateway):
        self.lifecycle=lifecycle;self.session=session;self.client=client
        self.sandbox_id=sandbox_id;self.attempt=attempt;self.local_closed=False
        self.revoke_gateway=revoke_gateway;self.gateway_revoked=False
        self.sandbox_deleted=False;self.cleanup_error=None;self.cleanup_lock=threading.RLock()
        self.is_remote=True

    def open_session(self,instructions,session_id=None):
        return self.client.call('session.open',{'instructions':instructions,'session_id':session_id})

    def run_turn(self,session_id,text):
        return self.client.call('turn.start',{'session_id':session_id,'text':text})

    def interrupt(self):return self.client.call('turn.interrupt',{})
    def export_session(self):
        result=self.client.call('session.export',{});reference=result.pop('manifest_ref',None)
        if reference:
            data=self.session.files.read(reference['path'])
            if len(data)!=reference['size'] or hashlib.sha256(data).hexdigest()!=reference['sha256']:
                raise ValueError('Runtime export manifest integrity mismatch')
            result['manifest']=json.loads(data)
        else:result.setdefault('manifest',{'workspace':{},'state':{}})
        return result
    def receive_once(self):return self.client.receive_once()
    def set_event_handler(self,handler):self.client.on_event=handler
    def revoked(self):return False
    def begin_cleanup(self):
        with self.cleanup_lock:
            if self.gateway_revoked:return
            try:self.revoke_gateway()
            except Exception as exc:self.cleanup_error=exc
            else:self.gateway_revoked=True;self.cleanup_error=None
    def pulse(self,seconds):pass
    def quiesce(self):
        try:self.interrupt()
        except Exception:pass
    def checkpoint(self):return self.export_session()
    def close(self):self.cleanup()
    def collect(self,manifest,sink):
        from .runtime_workspace import WorkspaceCollector
        return WorkspaceCollector(self.session.files).collect(manifest,sink)

    def cleanup(self):
        with self.cleanup_lock:
            self.begin_cleanup()
            if not self.local_closed:
                self.client.close();self.session.close();self.local_closed=True
            if not self.sandbox_deleted:
                self.lifecycle.delete_confirmed(self.sandbox_id,expected_attempt=self.attempt)
                self.sandbox_deleted=True
            if self.cleanup_error:raise self.cleanup_error


class OpenSandboxBackend:
    def __init__(self,lifecycle,sdk,*,runtime_client_factory=RuntimeClient):
        self.lifecycle=lifecycle;self.sdk=sdk;self.runtime_client_factory=runtime_client_factory

    @staticmethod
    def frozen_binding(binding,spec):
        return {'harness':spec.harness,'runtime_profile':binding.runtime_profile,
                'image_digest':binding.image_digest,'skill_digest':binding.skill_digest,
                'tool_schema_digest':spec.tool_schema_digest,
                'gateway_protocol':spec.gateway_protocol}

    def start(self,lease,binding,resources,spec,restore_workspace,on_event,*,timeout=3600):
        if binding.backend!=OPEN_SANDBOX or binding.runtime_kind!='agent_runtime':
            raise BackendConfigurationError('OpenSandboxBackend requires an agent_runtime binding')
        if spec.gateway_grant_factory is None:
            raise BackendConfigurationError('OpenSandbox Agent requires a Gateway grant factory')
        lease.start_compute()
        payload=self.lifecycle.create(lease,binding,resources,timeout=timeout,
            entrypoint=BOOTSTRAP_ENTRYPOINT)
        sandbox_id=(payload or {}).get('id')
        if not sandbox_id:raise OpenSandboxProvisioningError(ValueError('Create response has no sandbox id'))
        session=client=None;gateway_grant=None
        try:
            identity=RuntimeIdentity(lease.attempt,lease.generation,sandbox_id)
            gateway_grant=spec.gateway_grant_factory(identity)
            if not isinstance(gateway_grant,RuntimeGatewayGrant):
                raise BackendConfigurationError('Gateway grant factory returned an invalid grant')
            frozen=self.frozen_binding(binding,spec)
            bundle=RuntimeBundle.create(identity,spec.harness,
                {'model':spec.model,'reasoning_effort':spec.reasoning_effort,
                 'response_transport':spec.response_transport},
                frozen,spec.harness_config,gateway=gateway_grant.material)
            session=self.sdk.connect(sandbox_id)
            RuntimeInjector(session.files).inject(bundle,restore_workspace)
            endpoint,headers=session.websocket_endpoint(CONTROL_PORT)
            client=self.runtime_client_factory(endpoint,identity,bundle.control_token,on_event,
                headers=headers)
            client.connect();client.call('runtime.initialize',frozen)
            return OpenSandboxRuntime(self.lifecycle,session,client,sandbox_id,lease.attempt,
                gateway_grant.revoke)
        except Exception as exc:
            if gateway_grant is not None:
                try:gateway_grant.revoke()
                except Exception:pass
            if client is not None:client.close()
            if session is not None:session.close()
            cleanup_error=None
            try:self.lifecycle.delete_confirmed(sandbox_id,expected_attempt=lease.attempt)
            except Exception as failure:cleanup_error=failure
            raise OpenSandboxProvisioningError(exc,cleanup_error) from exc
