"""Bind a durable video route to its host-held credential without storing the key."""
import hashlib
import hmac
import json
import os
import re
from .egress_policy import provider_base


def credential_binding(handle,key):
    if not isinstance(key,str) or not key:raise ValueError('Original provider credential is unavailable')
    fields={name:handle.get(name) for name in ('provider','protocol','model','operation')}
    if any(not isinstance(value,str) or not value for value in fields.values()):
        raise ValueError('Incomplete frozen provider route')
    if provider_base(fields['provider'])!=fields['provider']:raise ValueError('Noncanonical frozen provider route')
    message=b'generation-route-v1\0'+json.dumps(fields,sort_keys=True,separators=(',',':')).encode()
    return hmac.new(key.encode(),message,hashlib.sha256).hexdigest()


def frozen_headers(handle,protocol,key_variable):
    if handle.get('protocol')!=protocol:raise ValueError('Frozen provider protocol mismatch')
    key=os.environ.get(key_variable,'');saved=handle.get('credential_binding')
    if not isinstance(saved,str) or not re.fullmatch(r'[a-f0-9]{64}',saved) or not hmac.compare_digest(saved,credential_binding(handle,key)):
        raise ValueError('Frozen provider route or credential changed; restore the original credential before querying')
    return {'Authorization':'Bearer '+key}
