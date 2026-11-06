"""Sandbox placement and OpenSandbox lifecycle primitives.

The first implementation freezes a binding in the executing lease without a
database migration.  Runtime allocation must honor it; an unavailable remote
backend never falls back to the host launcher because that would weaken the
selected isolation.
"""
from __future__ import annotations

from dataclasses import dataclass
import hashlib
import os
import re
from urllib.parse import quote, urlencode

import requests


LOCAL_BWRAP = 'local_bwrap'
OPEN_SANDBOX = 'opensandbox'
OPEN_SANDBOX_CLEANUP_PREFIX = 'opensandbox:'
BACKENDS = {LOCAL_BWRAP, OPEN_SANDBOX}


class BackendConfigurationError(ValueError):
    pass


class BackendNotReady(RuntimeError):
    pass


class AmbiguousProvisioning(RuntimeError):
    """The create request may have succeeded, so it must be reconciled."""


class DuplicateProvisioning(RuntimeError):
    pass


class DeletionPending(RuntimeError):
    pass


@dataclass(frozen=True)
class BackendBinding:
    backend: str
    runtime_profile: str
    image_digest: str = ''
    skill_digest: str = ''
    runtime_kind: str = LOCAL_BWRAP

    def __post_init__(self):
        if self.backend not in BACKENDS or self.runtime_kind not in (LOCAL_BWRAP,'agent_runtime','command_runtime'):
            raise BackendConfigurationError('Unknown sandbox backend: '+self.backend)


def _immutable_image(value):
    return bool(re.fullmatch(r'.+@sha256:[0-9a-fA-F]{64}', value or ''))


def select_backend(resources, *, environ=None, local_profile=''):
    """Choose a backend from frozen resource requirements and node config."""
    environ = os.environ if environ is None else environ
    if resources.get('gpu') or resources.get('host_devices'):
        return BackendBinding(LOCAL_BWRAP, local_profile or 'local')
    kind=resources.get('sandbox_kind','agent_runtime')
    selector=('VIDEO_USE_CPU_AGENT_SANDBOX_BACKEND' if kind=='agent_runtime' else
              'VIDEO_USE_CPU_COMMAND_SANDBOX_BACKEND' if kind=='command_runtime' else
              'VIDEO_USE_CPU_SANDBOX_BACKEND')
    backend = environ.get(selector,environ.get('VIDEO_USE_CPU_SANDBOX_BACKEND',LOCAL_BWRAP))
    if backend not in BACKENDS:
        raise BackendConfigurationError(
            'VIDEO_USE_CPU_SANDBOX_BACKEND must be local_bwrap or opensandbox')
    if backend == LOCAL_BWRAP:
        return BackendBinding(LOCAL_BWRAP, local_profile or 'local')
    if kind not in ('agent_runtime','command_runtime'):
        raise BackendConfigurationError('CPU task is not eligible for OpenSandbox')
    prefix='VIDEO_USE_OPEN_SANDBOX_AGENT_' if kind=='agent_runtime' else 'VIDEO_USE_OPEN_SANDBOX_COMMAND_'
    profile = environ.get(prefix+'RUNTIME_PROFILE', '')
    image = environ.get(prefix+'IMAGE_DIGEST', '')
    skills = environ.get(prefix+('SKILL_DIGEST' if kind=='agent_runtime' else 'BUNDLE_DIGEST'), '')
    missing = [name for name, value in (
        (prefix+'RUNTIME_PROFILE', profile),
        (prefix+'IMAGE_DIGEST', image),
        (prefix+('SKILL_DIGEST' if kind=='agent_runtime' else 'BUNDLE_DIGEST'), skills),
    ) if not value]
    if missing:
        raise BackendConfigurationError('OpenSandbox configuration missing: '+', '.join(missing))
    if not _immutable_image(image):
        raise BackendConfigurationError(
            prefix+'IMAGE_DIGEST must use image@sha256:<64 hex>')
    return BackendBinding(OPEN_SANDBOX,profile,image,skills,kind)


class RuntimeBackends:
    """Runtime factory registry keyed by an in-memory frozen binding."""
    def __init__(self, local_factory, opensandbox_factory=None,command_factory=None):
        self.local_factory=local_factory;self.opensandbox_factory=opensandbox_factory
        self.command_factory=command_factory

    def create(self, lease, binding):
        if binding.backend==LOCAL_BWRAP:
            return self.local_factory(lease)
        if binding.runtime_kind=='agent_runtime' and self.opensandbox_factory is not None:
            return self.opensandbox_factory(lease,binding)
        if binding.runtime_kind=='command_runtime' and self.command_factory is not None:
            return self.command_factory(lease,binding)
        raise BackendNotReady(
            'OpenSandbox execution transport is not enabled; refusing local fallback')


class RequestsTransport:
    def __init__(self, base_url, api_key='', timeout=10):
        self.base_url=base_url.rstrip('/')
        self.timeout=timeout
        self.session=requests.Session()
        self.session.trust_env=False
        if api_key:self.session.headers['OPEN-SANDBOX-API-KEY']=api_key

    def request(self, method, path, *, body=None, headers=None):
        response=self.session.request(method,self.base_url+path,json=body,
            headers=headers,timeout=self.timeout,allow_redirects=False)
        data=response.json() if response.content else None
        return response.status_code,data

    def close(self):
        self.session.close()


class OpenSandboxClient:
    """Small lifecycle client with deterministic attempt reconciliation."""
    def __init__(self, transport,*,network_policy='deny'):
        self.transport=transport
        if network_policy not in ('deny','internal_bridge'):
            raise BackendConfigurationError('Unknown OpenSandbox network isolation mode')
        self.network_policy=network_policy

    @staticmethod
    def metadata(lease, binding):
        short=lambda value:hashlib.sha256(value.encode()).hexdigest()[:32]
        return {'video-use.io/attempt':lease.attempt,
            'video-use.io/generation':str(lease.generation),
            'video-use.io/task-ref':short(lease.run_id),
            'video-use.io/profile-ref':short(binding.runtime_profile),
            'video-use.io/skill-ref':short(binding.skill_digest)}

    @staticmethod
    def _items(payload):
        return (payload or {}).get('items') or (payload or {}).get('sandboxes') or []

    def list_by_attempt(self, attempt):
        metadata=urlencode({'video-use.io/attempt':attempt})
        path='/v1/sandboxes?'+urlencode({'metadata':metadata,'pageSize':200})
        status,payload=self.transport.request('GET',path)
        if status!=200:raise RuntimeError('OpenSandbox list failed with HTTP '+str(status))
        return self._items(payload)

    def create(self, lease, binding, resources, *, timeout=3600,entrypoint=None):
        body={'image':{'uri':binding.image_digest},
            'timeout':max(60,int(timeout)),
            'resourceLimits':{'cpu':str(max(1,int(resources['cpu']))),
                              'memory':str(max(512,int(resources['memory_mb'])))+'Mi'},
            'metadata':self.metadata(lease,binding),
            'entrypoint':entrypoint or ['tail','-f','/dev/null']}
        if self.network_policy=='deny':body['networkPolicy']={'defaultAction':'deny','egress':[]}
        try:
            status,payload=self.transport.request('POST','/v1/sandboxes',body=body,
                headers={'X-Request-ID':lease.attempt})
        except (requests.ConnectionError,requests.Timeout,AmbiguousProvisioning) as exc:
            matches=self.list_by_attempt(lease.attempt)
            if len(matches)==1:return matches[0]
            if len(matches)>1:raise DuplicateProvisioning(
                'Multiple OpenSandbox instances found for attempt '+lease.attempt) from exc
            raise AmbiguousProvisioning(
                'OpenSandbox create outcome is unknown for attempt '+lease.attempt) from exc
        if status!=202:raise RuntimeError('OpenSandbox create failed with HTTP '+str(status))
        return payload

    def delete_confirmed(self, sandbox_id, *, expected_attempt):
        status,payload=self.transport.request('GET','/v1/sandboxes/'+quote(sandbox_id,safe=''))
        if status==404:return
        if status!=200:raise RuntimeError('OpenSandbox lookup failed with HTTP '+str(status))
        metadata=(payload or {}).get('metadata',{})
        if metadata.get('video-use.io/attempt')!=expected_attempt:
            raise BackendConfigurationError('Refusing to delete sandbox owned by another attempt')
        status,_=self.transport.request('DELETE','/v1/sandboxes/'+quote(sandbox_id,safe=''))
        if status not in (204,404):raise RuntimeError('OpenSandbox delete failed with HTTP '+str(status))
        status,_=self.transport.request('GET','/v1/sandboxes/'+quote(sandbox_id,safe=''))
        if status!=404:raise DeletionPending('OpenSandbox deletion has not converged for '+sandbox_id)


class OpenSandboxCleanupRegistry:
    """Persist sandbox ownership before create so a dead executor can be reaped."""
    @staticmethod
    def key(attempt):return OPEN_SANDBOX_CLEANUP_PREFIX+attempt

    def reserve(self,lease):
        key=self.key(lease.attempt)
        with lease.store.connect() as db:
            db.execute('BEGIN IMMEDIATE');lease.validate(db)
            db.execute("""INSERT INTO execution_external_cleanup
                (id,task_id,provider,external_id,state,phase,error)
                VALUES (?,?,?,'','dormant','delete','') ON CONFLICT(id) DO NOTHING""",
                (key,lease.run_id,key))
        return key

    def attach(self,lease,sandbox_id):
        changed=lease.store.execute("UPDATE execution_external_cleanup SET external_id=? WHERE id=? AND state='dormant'",
            (sandbox_id,self.key(lease.attempt)))
        if changed!=1:raise RuntimeError('OpenSandbox cleanup ownership was lost')

    def complete(self,store,attempt):
        store.execute("UPDATE execution_external_cleanup SET state='complete',phase='deleted',lease_until=0,next_retry=0,error='' WHERE id=?",
            (self.key(attempt),))
