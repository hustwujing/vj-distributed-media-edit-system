"""OpenSandbox Agent runtime orchestration without business-state ownership."""
from __future__ import annotations

from dataclasses import dataclass

from .runtime_bootstrap import CONTROL_PORT
from .runtime_client import RuntimeClient
from .runtime_injection import RuntimeBundle,RuntimeInjector
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
    tool_schema_digest: str
    gateway_protocol: str
    harness_config: dict


class OpenSandboxRuntime:
    def __init__(self,lifecycle,session,client,sandbox_id,attempt):
        self.lifecycle=lifecycle;self.session=session;self.client=client
        self.sandbox_id=sandbox_id;self.attempt=attempt;self.local_closed=False

    def open_session(self,instructions,session_id=None):
        return self.client.call('session.open',{'instructions':instructions,'session_id':session_id})

    def run_turn(self,session_id,text):
        return self.client.call('turn.start',{'session_id':session_id,'text':text})

    def interrupt(self):return self.client.call('turn.interrupt',{})
    def export_session(self):return self.client.call('session.export',{})
    def receive_once(self):return self.client.receive_once()

    def cleanup(self):
        if not self.local_closed:
            self.client.close();self.session.close();self.local_closed=True
        self.lifecycle.delete_confirmed(self.sandbox_id,expected_attempt=self.attempt)


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
        if binding.backend!=OPEN_SANDBOX:
            raise BackendConfigurationError('OpenSandboxBackend requires an opensandbox binding')
        payload=self.lifecycle.create(lease,binding,resources,timeout=timeout,
            entrypoint=BOOTSTRAP_ENTRYPOINT)
        sandbox_id=(payload or {}).get('id')
        if not sandbox_id:raise OpenSandboxProvisioningError(ValueError('Create response has no sandbox id'))
        session=client=None
        try:
            identity=RuntimeIdentity(lease.attempt,lease.generation,sandbox_id)
            frozen=self.frozen_binding(binding,spec)
            bundle=RuntimeBundle.create(identity,spec.harness,
                {'model':spec.model,'reasoning_effort':spec.reasoning_effort},
                frozen,spec.harness_config)
            session=self.sdk.connect(sandbox_id)
            RuntimeInjector(session.files).inject(bundle,restore_workspace)
            endpoint,headers=session.websocket_endpoint(CONTROL_PORT)
            client=self.runtime_client_factory(endpoint,identity,bundle.control_token,on_event,
                headers=headers)
            client.connect();client.call('runtime.initialize',frozen)
            return OpenSandboxRuntime(self.lifecycle,session,client,sandbox_id,lease.attempt)
        except Exception as exc:
            if client is not None:client.close()
            if session is not None:session.close()
            cleanup_error=None
            try:self.lifecycle.delete_confirmed(sandbox_id,expected_attempt=lease.attempt)
            except Exception as failure:cleanup_error=failure
            raise OpenSandboxProvisioningError(exc,cleanup_error) from exc
