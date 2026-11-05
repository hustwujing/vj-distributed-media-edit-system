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

    def __post_init__(self):
        if self.backend not in BACKENDS:
            raise BackendConfigurationError('Unknown sandbox backend: '+self.backend)


def _immutable_image(value):
    return bool(re.fullmatch(r'.+@sha256:[0-9a-fA-F]{64}', value or ''))


def select_backend(resources, *, environ=None, local_profile=''):
    """Choose a backend from frozen resource requirements and node config."""
    environ = os.environ if environ is None else environ
    if resources.get('gpu') or resources.get('host_devices'):
        return BackendBinding(LOCAL_BWRAP, local_profile or 'local')
    backend = environ.get('VIDEO_USE_CPU_SANDBOX_BACKEND', LOCAL_BWRAP)
    if backend not in BACKENDS:
        raise BackendConfigurationError(
            'VIDEO_USE_CPU_SANDBOX_BACKEND must be local_bwrap or opensandbox')
    if backend == LOCAL_BWRAP:
        return BackendBinding(LOCAL_BWRAP, local_profile or 'local')
    profile = environ.get('VIDEO_USE_OPEN_SANDBOX_RUNTIME_PROFILE', '')
    image = environ.get('VIDEO_USE_OPEN_SANDBOX_IMAGE_DIGEST', '')
    skills = environ.get('VIDEO_USE_OPEN_SANDBOX_SKILL_DIGEST', '')
    missing = [name for name, value in (
        ('VIDEO_USE_OPEN_SANDBOX_RUNTIME_PROFILE', profile),
        ('VIDEO_USE_OPEN_SANDBOX_IMAGE_DIGEST', image),
        ('VIDEO_USE_OPEN_SANDBOX_SKILL_DIGEST', skills),
    ) if not value]
    if missing:
        raise BackendConfigurationError('OpenSandbox configuration missing: '+', '.join(missing))
    if not _immutable_image(image):
        raise BackendConfigurationError(
            'VIDEO_USE_OPEN_SANDBOX_IMAGE_DIGEST must use image@sha256:<64 hex>')
    return BackendBinding(OPEN_SANDBOX, profile, image, skills)


class RuntimeBackends:
    """Runtime factory registry keyed by an in-memory frozen binding."""
    def __init__(self, local_factory):
        self.local_factory=local_factory

    def create(self, lease, binding):
        if binding.backend==LOCAL_BWRAP:
            return self.local_factory(lease)
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
    def __init__(self, transport):
        self.transport=transport

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

    def create(self, lease, binding, resources, *, timeout=3600):
        body={'image':{'uri':binding.image_digest},
            'timeout':max(60,int(timeout)),
            'resourceLimits':{'cpu':str(max(1,int(resources['cpu']))),
                              'memory':str(max(512,int(resources['memory_mb'])))+'Mi'},
            'metadata':self.metadata(lease,binding),
            'entrypoint':['tail','-f','/dev/null'],
            'networkPolicy':{'defaultAction':'deny','egress':[]}}
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
