"""Trusted image entrypoint that wires Runtime Daemon to one frozen Harness."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import time
from types import SimpleNamespace

from .harness_adapters import get as harness_adapter
from .runtime_daemon import create_app
from .runtime_harness import HarnessController,StdioHarnessBridge
from .runtime_protocol import RuntimeIdentity


CONTROL_PORT=18770
BINDING_KEYS={'harness','runtime_profile','image_digest','skill_digest',
              'tool_schema_digest','gateway_protocol'}
RUNTIME_BUNDLE=Path('/opt/video-use')


def _secure_file(path,root,maximum):
    path=Path(path)
    try:path.resolve().relative_to(root.resolve())
    except ValueError as exc:raise ValueError('Runtime file is outside the trusted directory') from exc
    info=path.lstat()
    if not stat.S_ISREG(info.st_mode) or info.st_uid!=os.getuid() or info.st_mode&0o077:
        raise ValueError('Runtime file must be owner-only and regular')
    if info.st_size>maximum:raise ValueError('Runtime file exceeds size limit')
    return path


def verify_runtime_bundle(binding,root=RUNTIME_BUNDLE):
    root=Path(root);manifest_path=root/'bundle.json'
    manifest=json.loads(manifest_path.read_text())
    if (not isinstance(manifest,dict) or set(manifest)!={'schema_version','skill_digest','tool_schema_digest','files'}
            or manifest['schema_version']!=1 or not isinstance(manifest['files'],list)):
        raise ValueError('Invalid Runtime Skill Bundle manifest')
    records=[];seen=set()
    for record in manifest['files']:
        if not isinstance(record,dict) or set(record)!={'path','size','sha256'}:
            raise ValueError('Invalid Runtime Skill Bundle record')
        if not isinstance(record['path'],str):raise ValueError('Invalid Runtime Skill Bundle path')
        relative=Path(record['path'])
        if relative.is_absolute() or '..' in relative.parts or relative.as_posix() in seen:
            raise ValueError('Invalid Runtime Skill Bundle path')
        seen.add(relative.as_posix());path=root/relative
        if path.is_symlink() or not path.is_file() or path.stat().st_size!=record['size']:
            raise ValueError('Runtime Skill Bundle file mismatch')
        digest=hashlib.sha256(path.read_bytes()).hexdigest()
        if digest!=record['sha256']:raise ValueError('Runtime Skill Bundle digest mismatch')
        records.append(record)
    if records!=sorted(records,key=lambda item:item['path']):
        raise ValueError('Runtime Skill Bundle records are not canonical')
    encoded=json.dumps(records,sort_keys=True,separators=(',',':')).encode()
    skill='sha256:'+hashlib.sha256(encoded).hexdigest()
    tools=next((item for item in records if item['path']=='capabilities.json'),None)
    tool_digest='sha256:'+tools['sha256'] if tools else ''
    if (skill!=manifest['skill_digest'] or tool_digest!=manifest['tool_schema_digest'] or
            binding['skill_digest']!=skill or binding['tool_schema_digest']!=tool_digest):
        raise ValueError('Frozen Runtime binding does not match the Skill Bundle')
    return manifest


def load_config(path,*,bundle_root=RUNTIME_BUNDLE):
    path=Path(path);root=path.resolve().parent
    path=_secure_file(path,root,64*1024)
    value=json.loads(path.read_text())
    required={'identity','harness','profile','binding','control_token_file','harness_config_file'}
    if not isinstance(value,dict) or not required<=set(value) or set(value)-required-{'gateway_config_file'}:
        raise ValueError('Invalid Runtime bootstrap configuration')
    identity=value['identity'];profile=value['profile'];binding=value['binding']
    if not isinstance(identity,dict) or set(identity)!={'attempt_id','generation','sandbox_id'}:
        raise ValueError('Invalid Runtime identity configuration')
    runtime_identity=RuntimeIdentity(**identity)
    adapter=harness_adapter(value['harness'])
    if not isinstance(profile,dict) or set(profile)!={'model','reasoning_effort','response_transport'}:
        raise ValueError('Invalid Harness profile configuration')
    if any(not isinstance(profile[key],str) or not profile[key] or len(profile[key])>200 for key in profile):
        raise ValueError('Invalid Harness profile value')
    if profile['response_transport'] not in ('buffered','stream'):
        raise ValueError('Invalid Harness response transport')
    if (not isinstance(binding,dict) or set(binding)!=BINDING_KEYS or
            binding['harness']!=adapter.id or
            any(not isinstance(item,str) or not item or len(item)>300 for item in binding.values())):
        raise ValueError('Invalid frozen Runtime binding')
    if (not re.fullmatch(r'.+@sha256:[0-9a-f]{64}',binding['image_digest']) or
            any(not re.fullmatch(r'sha256:[0-9a-f]{64}',binding[key])
                for key in ('skill_digest','tool_schema_digest')) or
            not re.fullmatch(r'[A-Za-z0-9_.-]{1,128}',binding['runtime_profile']) or
            not re.fullmatch(r'[A-Za-z0-9_.-]{1,64}',binding['gateway_protocol'])):
        raise ValueError('Invalid frozen Runtime digest or protocol')
    verify_runtime_bundle(binding,bundle_root)
    token_file=_secure_file(value['control_token_file'],root,4096)
    token=token_file.read_text().strip()
    if not 32<=len(token)<=512:raise ValueError('Invalid Runtime control token')
    harness_config=_secure_file(value['harness_config_file'],root,64*1024)
    return {'identity':runtime_identity,'adapter':adapter,
            'profile':SimpleNamespace(**profile),'binding':binding,'token':token,
            'harness_config':str(harness_config),
            'gateway_config':value.get('gateway_config_file')}


def load_gateway(path,identity):
    path=Path(path);root=path.resolve().parent
    path=_secure_file(path,root,64*1024);value=json.loads(path.read_text())
    required={'identity','endpoint','allowed_routes','token_file','ca_file'}
    if not isinstance(value,dict) or set(value)!=required or value['identity']!={
            'attempt_id':identity.attempt_id,'generation':identity.generation,'sandbox_id':identity.sandbox_id}:
        raise ValueError('Invalid Gateway grant configuration')
    token_file=_secure_file(value['token_file'],root,4096)
    ca_file=_secure_file(value['ca_file'],root,1024*1024)
    return value['endpoint'],token_file.read_text().strip(),value['allowed_routes'],str(ca_file)


def create_runtime(path,*,gateway_port=18766,bundle_root=RUNTIME_BUNDLE):
    config=load_config(path,bundle_root=bundle_root);holder={}
    harness_config=json.loads(Path(config['harness_config']).read_text())
    if (not isinstance(harness_config,dict)
            or harness_config.get('response_transport')!=config['profile'].response_transport):
        raise ValueError('Harness response transport does not match frozen profile')
    environment={'PYTHONPATH':'/opt/harness:/opt/video-use:/opt/video-use/helpers'}
    workspace=harness_config.get('task_workspace')
    if workspace is not None:
        from pathlib import PurePosixPath
        def scoped(value):
            return isinstance(value,str) and value.startswith('/workspace/') and '..' not in PurePosixPath(value).parts
        if (not isinstance(workspace,dict) or set(workspace)!={'output','inputs'}
                or not scoped(workspace['output']) or not workspace['output'].startswith('/workspace/edit/workflows/')
                or not isinstance(workspace['inputs'],dict) or any(not isinstance(paths,list) or not paths
                    or any(not scoped(path) for path in paths) for paths in workspace['inputs'].values())):
            raise ValueError('Invalid frozen Task workspace contract')
        from .sandbox_path_policy import scratch_directory
        environment.update(VIDEO_USE_STEP_OUTPUT=workspace['output'],
            VIDEO_USE_STEP_SCRATCH=scratch_directory(workspace['output']),
            VIDEO_USE_STEP_INPUTS=json.dumps(workspace['inputs'],sort_keys=True))
    bridge=StdioHarnessBridge(config['adapter'],
        config['adapter'].runtime_launch(config['harness_config']),environment=environment)
    from .runtime_workspace import WorkspaceExporter
    controller=HarnessController(config['adapter'],config['profile'],bridge,
        lambda event:holder['service'].publish(event),config['binding'],
        WorkspaceExporter())
    app=create_app(config['identity'],config['token'],controller.dispatch,
        lambda:{'ready':True,'state':controller.state,'harness':config['adapter'].id,
                'runtime_profile':config['binding']['runtime_profile']})
    holder['service']=app.state.runtime_service
    app.state.harness_controller=controller
    app.router.add_event_handler('shutdown',controller.close)
    app.state.gateway_server=None
    if config['gateway_config']:
        from .runtime_gateway import GatewayClient,GatewayClientServer
        endpoint,token,routes,ca=load_gateway(config['gateway_config'],config['identity'])
        gateway=GatewayClient(endpoint,token,config['identity'],routes,ca)
        server=GatewayClientServer(gateway,port=gateway_port);server.start()
        app.state.gateway_server=server
        app.router.add_event_handler('shutdown',server.close)
    return app


def wait_for_config(path,seconds):
    deadline=time.monotonic()+seconds;path=Path(path)
    while time.monotonic()<deadline:
        if path.exists():return path
        time.sleep(.1)
    raise TimeoutError('Runtime bootstrap configuration was not injected')


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('--config',default='/run/video-use/runtime.json')
    parser.add_argument('--wait-seconds',type=int,default=300)
    args=parser.parse_args()
    if not 1<=args.wait_seconds<=900:parser.error('wait-seconds must be between 1 and 900')
    app=create_runtime(wait_for_config(args.config,args.wait_seconds))
    import uvicorn
    # Reachable only through the private sandbox network/proxy. Every control
    # frame still requires the Attempt-bound token and generation.
    uvicorn.run(app,host='0.0.0.0',port=CONTROL_PORT,ws_max_size=1024*1024,
                access_log=False,server_header=False)


if __name__=='__main__':main()
