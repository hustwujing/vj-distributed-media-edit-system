"""Registered runners for frozen specialist policies, separate from model transports."""
from typing import Protocol
from functools import lru_cache
import hashlib
from pathlib import Path


@lru_cache(maxsize=2048)
def _source_digest(path,signature):
    """Deployed sources are immutable; stat identity also detects local edits."""
    return hashlib.sha256(path.read_bytes()).digest()


class ProfileHarness(Protocol):
    id:str
    def run(self,host,lease,workflow,task):...


class StructuredProfileHarness:
    id='structured-profile-v4'
    def fingerprint(self):
        root=Path(__file__).parent;value=hashlib.sha256()
        # Bind the entire trusted Python package, including lazy imports. A
        # conservative package boundary cannot drift as execution dependencies
        # move; UI assets and skill text have their own independent identities.
        for path in sorted(root.rglob('*.py')):
            stat=path.stat()
            signature=(stat.st_dev,stat.st_ino,stat.st_size,stat.st_mtime_ns,stat.st_ctime_ns)
            value.update(path.relative_to(root).as_posix().encode()+b'\0')
            value.update(_source_digest(path,signature))
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
