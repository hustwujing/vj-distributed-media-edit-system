"""Ordered Runtime configuration injection through a sandbox Files adapter."""
from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import secrets

from .runtime_protocol import RuntimeIdentity


RUNTIME_ROOT='/run/video-use'
RUNTIME_CONFIG=RUNTIME_ROOT+'/runtime.json'
RUNTIME_PENDING=RUNTIME_ROOT+'/runtime.json.pending'
CONTROL_TOKEN=RUNTIME_ROOT+'/control.token'
HARNESS_CONFIG=RUNTIME_ROOT+'/harness-config.json'


@dataclass(frozen=True)
class RuntimeBundle:
    identity: RuntimeIdentity
    harness: str
    profile: dict
    binding: dict
    harness_config: dict
    control_token: str

    @classmethod
    def create(cls,identity,harness,profile,binding,harness_config,token=None):
        return cls(identity,harness,dict(profile),dict(binding),dict(harness_config),
                   token or secrets.token_urlsafe(32))

    def runtime_config(self):
        return {'identity':{'attempt_id':self.identity.attempt_id,
                            'generation':self.identity.generation,
                            'sandbox_id':self.identity.sandbox_id},
                'harness':self.harness,'profile':self.profile,'binding':self.binding,
                'control_token_file':CONTROL_TOKEN,'harness_config_file':HARNESS_CONFIG}


class RuntimeInjector:
    """Activate a daemon only after all immutable inputs verify successfully.

    ``files`` deliberately uses a tiny adapter contract: mkdir(path, mode),
    write(path, bytes, mode), read(path) and delete(path).  The OpenSandbox SDK
    adapter can implement this without leaking SDK types into scheduling code.
    """
    def __init__(self,files):self.files=files

    @staticmethod
    def _encoded(value):
        return json.dumps(value,sort_keys=True,separators=(',',':')).encode()

    def _write_verified(self,path,data,mode):
        self.files.write(path,data,mode)
        actual=self.files.read(path)
        if actual!=data:
            raise IOError('Sandbox file verification failed: '+path)
        return hashlib.sha256(data).hexdigest()

    def inject(self,bundle,restore_workspace):
        self.files.delete(RUNTIME_CONFIG,missing_ok=True)
        self.files.delete(RUNTIME_PENDING,missing_ok=True)
        for path,mode in ((RUNTIME_ROOT,0o700),('/workspace',0o700),('/state',0o700)):
            self.files.mkdir(path,mode)
        restore_workspace(self.files)
        harness=self._encoded(bundle.harness_config)
        token=bundle.control_token.encode()
        if not 32<=len(token)<=512:raise ValueError('Invalid Runtime control token')
        receipts={HARNESS_CONFIG:self._write_verified(HARNESS_CONFIG,harness,0o600),
                  CONTROL_TOKEN:self._write_verified(CONTROL_TOKEN,token,0o600)}
        runtime=self._encoded(bundle.runtime_config())
        if bundle.control_token.encode() in runtime:
            raise ValueError('Runtime configuration must not contain the control token')
        # Bootstrap waits on RUNTIME_CONFIG.  Verify a staging file, then use a
        # same-filesystem rename as the activation commit marker.
        receipts[RUNTIME_CONFIG]=self._write_verified(RUNTIME_PENDING,runtime,0o600)
        self.files.move(RUNTIME_PENDING,RUNTIME_CONFIG)
        return receipts
