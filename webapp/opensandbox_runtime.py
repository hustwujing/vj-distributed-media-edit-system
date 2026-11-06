"""Production factory wiring for the configured OpenSandbox lifecycle API."""
from __future__ import annotations

import os

from .opensandbox_command import OpenSandboxCommandBackend,OpenSandboxCommandFactory
from .opensandbox_sdk import OpenSandboxSDK
from .sandbox_backends import (BackendConfigurationError,OpenSandboxClient,
    RequestsTransport)


def command_factory_from_environment(environ=None):
    environ=os.environ if environ is None else environ
    selected=environ.get('VIDEO_USE_CPU_COMMAND_SANDBOX_BACKEND',
        environ.get('VIDEO_USE_CPU_SANDBOX_BACKEND','local_bwrap'))
    if selected!='opensandbox':return None
    base_url=environ.get('VIDEO_USE_OPEN_SANDBOX_URL','').rstrip('/')
    api_key=environ.get('VIDEO_USE_OPEN_SANDBOX_API_KEY','')
    isolation=environ.get('VIDEO_USE_OPEN_SANDBOX_NETWORK_ISOLATION','deny')
    if not base_url or not api_key:
        raise BackendConfigurationError('OpenSandbox URL and API key are required')
    if isolation=='internal_bridge' and environ.get('VIDEO_USE_OPEN_SANDBOX_INTERNAL_BRIDGE_CONFIRMED')!='1':
        raise BackendConfigurationError('Internal bridge isolation must be explicitly confirmed')
    transport=RequestsTransport(base_url,api_key,timeout=30)
    lifecycle=OpenSandboxClient(transport,network_policy=isolation)
    sdk=OpenSandboxSDK(base_url,api_key)
    return OpenSandboxCommandFactory(OpenSandboxCommandBackend(lifecycle,sdk))
