"""Server-owned routing capabilities for delegated code editors."""
from __future__ import annotations

import hashlib
import ipaddress
import json
import os
from pathlib import Path
from urllib.parse import urlsplit


CATALOG=Path(__file__).resolve().parents[1]/'runtime/public/capabilities.json'


def catalogue():
    value=json.loads(CATALOG.read_text()).get('agent_execution')
    required={'opensandbox_cpu','bwrap_cpu','bwrap_gpu'}
    if not isinstance(value,dict) or set(value)!=required:raise ValueError('Invalid Agent runtime capability catalogue')
    for name,item in value.items():
        if (not isinstance(item,dict) or set(item)!={'execution_location','requires_gpu','network_modes','python_modules','node_modules','system_tools'}
                or type(item['requires_gpu']) is not bool
                or item['requires_gpu']!=(name=='bwrap_gpu')
                or not all(isinstance(values,list) and len(values)==len(set(values))
                           for values in (item['network_modes'],item['python_modules'],item['node_modules'],item['system_tools']))):
            raise ValueError('Invalid Agent runtime capability record: '+name)
    from .native_skill_catalog import identifiers,GATEWAY_REQUIREMENTS
    result=json.loads(json.dumps(value))
    for name,item in result.items():
        item['native_skills']=list(identifiers()) if name=='opensandbox_cpu' else []
        item['gateway_operations']=sorted({operation for operations in GATEWAY_REQUIREMENTS.values() for operation in operations}) if name=='opensandbox_cpu' else []
        item['native_skill_gateway_requirements']={key:list(value) for key,value in sorted(GATEWAY_REQUIREMENTS.items())} if name=='opensandbox_cpu' else {}
    return result


def digest():
    return hashlib.sha256(json.dumps(catalogue(),sort_keys=True,separators=(',',':')).encode()).hexdigest()


def native_enabled(environ=None):
    env=os.environ if environ is None else environ
    return env.get('VIDEO_USE_CPU_AGENT_SANDBOX_BACKEND',env.get('VIDEO_USE_CPU_SANDBOX_BACKEND','local_bwrap'))=='opensandbox'


def native_harnesses(environ=None):
    env=os.environ if environ is None else environ
    raw=env.get('VIDEO_USE_OPEN_SANDBOX_AGENT_PROFILES_JSON','')
    if not raw:return {'codex','claude_code','deepseek_harness'}
    try:value=json.loads(raw)
    except ValueError:raise ValueError('Invalid native Harness Runtime Profile registry') from None
    if not isinstance(value,dict):raise ValueError('Invalid native Harness Runtime Profile registry')
    allowed={'codex','claude_code','deepseek_harness'}
    if set(value)-allowed:raise ValueError('Unknown native Harness Runtime Profile')
    return set(value)


def native_harness_enabled(harness,environ=None):
    return harness in native_harnesses(environ)


def target(requirements,environ=None):
    if requirements.get('requires_gpu'):return 'bwrap_gpu'
    return 'opensandbox_cpu' if native_enabled(environ) else 'bwrap_cpu'


def validate_public_url(value):
    if not isinstance(value,str) or len(value)>2048:raise ValueError('Approved network URL is invalid')
    parsed=urlsplit(value)
    if (parsed.scheme!='https' or not parsed.hostname or parsed.username or parsed.password or parsed.fragment
            or parsed.port not in (None,443)):
        raise ValueError('Approved network URLs must be exact public HTTPS URLs on port 443')
    hostname=parsed.hostname.lower()
    if hostname in ('localhost','metadata.google.internal','metadata.google','instance-data'):
        raise ValueError('Internal or metadata network destinations are forbidden')
    try:address=ipaddress.ip_address(hostname)
    except ValueError:address=None
    if address and not address.is_global:raise ValueError('Network destination must be public')
    return value


def validate(requirements,environ=None,selected=None):
    selected=selected or target(requirements,environ);available=catalogue()[selected]
    missing_modules=sorted(set(requirements.get('python_modules',()))-set(available['python_modules']))
    missing_node=sorted(set(requirements.get('node_modules',()))-set(available['node_modules']))
    missing_tools=sorted(set(requirements.get('system_tools',()))-set(available['system_tools']))
    network_urls=requirements.get('network_urls',())
    native_skills=requirements.get('native_skills',())
    gateway_operations=requirements.get('gateway_operations',())
    if native_skills and selected!='opensandbox_cpu':raise ValueError('Selected runtime does not support Native Skills')
    from .native_skill_catalog import validate as validate_native_skills
    validate_native_skills(native_skills)
    if network_urls and 'approved_public_https' not in available['network_modes']:
        raise ValueError('Selected runtime does not support approved public HTTPS')
    for value in network_urls:validate_public_url(value)
    unsupported_gateway=sorted(set(gateway_operations)-set(available['gateway_operations']))
    if unsupported_gateway:raise ValueError('Selected runtime lacks Gateway operations: '+', '.join(unsupported_gateway))
    if missing_modules or missing_node or missing_tools:
        detail=[]
        if missing_modules:detail.append('Python modules: '+', '.join(missing_modules))
        if missing_node:detail.append('Node modules: '+', '.join(missing_node))
        if missing_tools:detail.append('system tools: '+', '.join(missing_tools))
        raise ValueError('Selected runtime lacks declared dependencies ('+'; '.join(detail)+')')
    return selected
