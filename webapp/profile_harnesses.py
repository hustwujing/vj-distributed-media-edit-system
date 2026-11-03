"""Registered runners for frozen specialist policies, separate from model transports."""
from typing import Protocol


class ProfileHarness(Protocol):
    id:str
    def run(self,host,lease,workflow,task):...


class StructuredProfileHarness:
    id='structured-profile-v2'
    def fingerprint(self):
        from pathlib import Path
        import hashlib
        root=Path(__file__).parent;value=hashlib.sha256()
        for name in ('profile_harnesses.py','workflow_agents.py','workflow_agent_contracts.py','specialist_profiles.py'):
            value.update(name.encode());value.update((root/name).read_bytes())
        return value.hexdigest()
    def run(self,host,lease,workflow,task):
        from .workflow_agents import execute
        return execute(host,lease,workflow,task)


REGISTRY={StructuredProfileHarness.id:StructuredProfileHarness()}


def register(harness):
    import re
    if not re.fullmatch(r'[a-z][a-z0-9-]{0,63}',harness.id) or not callable(getattr(harness,'run',None)) or not callable(getattr(harness,'fingerprint',None)):
        raise ValueError('Invalid profile harness contract')
    if harness.id in REGISTRY:raise ValueError('Profile harness already registered')
    REGISTRY[harness.id]=harness


def get(identifier):
    try:return REGISTRY[identifier]
    except KeyError:raise ValueError('Profile harness is not available') from None
