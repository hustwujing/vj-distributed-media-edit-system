#!/usr/bin/env python3
"""Generate local-only OpenSandbox release evidence without deploying it."""
from __future__ import annotations

import argparse
from datetime import datetime,timezone
import hashlib
import json
import math
import os
from pathlib import Path,PurePosixPath
import shutil
import subprocess


ROOT=Path(__file__).resolve().parents[1]
SPEC=ROOT/'runtime/opensandbox/release-spec.json'
PUBLIC=ROOT/'runtime/public'
DEFAULT_OUTPUT=ROOT/'runtime/build/opensandbox-release.json'
MODEL_ROUTES={'codex':{'/responses','/v1/responses'},
    'claude_code':{'/messages','/v1/messages'},
    'deepseek_harness':{'/messages','/v1/messages'}}
GATEWAY_CAPABILITIES={'model','context','workflow','media_tools'}


def sha256(path):
    with Path(path).open('rb') as stream:return hashlib.file_digest(stream,'sha256').hexdigest()


def file_record(path,root=None):
    path=Path(path)
    return {'path':path.relative_to(root).as_posix() if root else path.name,
            'size':path.stat().st_size,'sha256':sha256(path)}


def bundle_record(root,names):
    records=[]
    for item in names:
        if isinstance(item,str):source=target=item
        else:source,target=item['source'],item['target']
        record=file_record(Path(root)/source);record['path']=target;records.append(record)
    records.sort(key=lambda item:item['path'])
    encoded=json.dumps(records,sort_keys=True,separators=(',',':')).encode()
    return {'digest':'sha256:'+hashlib.sha256(encoded).hexdigest(),'files':records}


def load_spec(path=SPEC):
    value=json.loads(Path(path).read_text())
    required={'schema_version','gateway_protocol','daemon_protocol','foundation','skill_bundle','profiles'}
    if set(value)!=required or value['schema_version']!=1 or not value['profiles']:
        raise ValueError('Invalid runtime release spec')
    tags=set()
    for name,profile in value['profiles'].items():
        if not name or profile['runtime_kind'] not in ('command_runtime','agent_runtime'):
            raise ValueError('Invalid runtime profile')
        if profile['tag'] in tags:raise ValueError('Duplicate runtime image tag')
        tags.add(profile['tag'])
        if profile['runtime_kind']=='agent_runtime' and not profile.get('harness'):
            raise ValueError('Agent runtime profile requires a harness')
        if profile['runtime_kind']=='command_runtime' and (profile.get('harness') or profile['model_routes']):
            raise ValueError('Command runtime cannot declare Agent routes')
        if profile.get('harness') and set(profile['model_routes'])!=MODEL_ROUTES.get(profile['harness']):
            raise ValueError('Harness model routes do not match the Gateway protocol')
        if set(profile['gateway_capabilities'])-GATEWAY_CAPABILITIES:
            raise ValueError('Unknown Gateway capability')
    if any(not isinstance(item,dict) or set(item)!={'source','target'} for item in value['skill_bundle']):
        raise ValueError('Invalid Skill Bundle member')
    sources=[item['source'] for item in value['skill_bundle']]
    targets=[item['target'] for item in value['skill_bundle']]
    paths=[PurePosixPath(path) for path in sources+targets if isinstance(path,str)]
    if (len(paths)!=len(sources)+len(targets) or len(set(targets))!=len(targets) or
            any(path.is_absolute() or '..' in path.parts for path in paths) or
            any(not (ROOT/source).is_file() for source in sources)):
        raise ValueError('Invalid Skill Bundle path')
    return value


def run_json(command):
    return json.loads(subprocess.check_output(command,text=True))


def inspect_images(docker,tags):
    values=run_json([docker,'image','inspect',*tags]);result={}
    for tag,value in zip(tags,values):
        config=value['Config'];history=subprocess.check_output(
            [docker,'history','--no-trunc','--format','{{.CreatedBy}}',tag],text=True).splitlines()
        encoded='\n'.join(history)+'\n'+json.dumps(config.get('Env') or [],sort_keys=True)
        forbidden=[name for name in ('API_KEY=','SECRET_ACCESS_KEY=','DATABASE_URL=') if name in encoded]
        result[tag]={'local_image_id':value['Id'],'size':value['Size'],
            'user':config.get('User'),'entrypoint':config.get('Entrypoint'),
            'command':config.get('Cmd'),'environment_names':sorted(
                item.split('=',1)[0] for item in config.get('Env') or []),
            'secret_marker_findings':forbidden}
    return result


def os_package_lock(docker,tag):
    output=subprocess.check_output([docker,'run','--rm','--network','none','--read-only',
        '--entrypoint','/usr/bin/dpkg-query',tag,'-W','-f=${Package}\t${Version}\n'],text=True)
    return [{'name':name,'version':version} for name,version in
        (line.split('\t',1) for line in output.splitlines())]


def git_state():
    commit=subprocess.check_output(['git','rev-parse','HEAD'],cwd=ROOT,text=True).strip()
    dirty=bool(subprocess.check_output(['git','status','--porcelain'],cwd=ROOT,text=True))
    return {'commit':commit,'dirty':dirty}


def manifest(docker,spec_path=SPEC,*,profiles=None):
    spec=load_spec(spec_path)
    omitted=[]
    if profiles is not None:
        if not profiles or not set(profiles)<=set(spec['profiles']):raise ValueError('Unknown or empty targeted Runtime Profile selection')
        omitted=sorted(set(spec['profiles'])-set(profiles))
        spec={**spec,'profiles':{name:value for name,value in spec['profiles'].items() if name in profiles}}
    skills=bundle_record(ROOT,spec['skill_bundle'])
    frozen=json.loads((PUBLIC/'bundle.json').read_text())
    if (frozen.get('skill_digest')!=skills['digest'] or
            frozen.get('tool_schema_digest')!='sha256:'+sha256(PUBLIC/'capabilities.json') or
            frozen.get('files')!=skills['files']):
        raise ValueError('Checked-in Runtime Skill Bundle manifest is stale')
    wheels=[file_record(path,ROOT/'wheels') for path in sorted((ROOT/'wheels').glob('*.whl'))]
    vendor=[file_record(path,ROOT/'vendor') for path in sorted((ROOT/'vendor').glob('*')) if path.is_file()]
    tags=[profile['tag'] for profile in spec['profiles'].values()]
    images=inspect_images(docker,tags)
    foundation=inspect_images(docker,[spec['foundation']['tag']])[spec['foundation']['tag']]
    tool_schema='sha256:'+sha256(PUBLIC/'capabilities.json')
    profiles={}
    for name,profile in spec['profiles'].items():
        image=images[profile['tag']]
        profiles[name]={**profile,'local_image_id':image['local_image_id'],
            'publish_reference':profile['publish_name']+'@sha256:<registry-manifest>',
            'publication_state':'local_only','skill_digest':skills['digest'],
            'tool_schema_digest':tool_schema,'gateway_protocol':spec['gateway_protocol'],
            'daemon_protocol':spec['daemon_protocol']}
    validation={name:{'passed':images[profile['tag']]['user']=='10001:10001' and
            images[profile['tag']]['entrypoint']==['/opt/venv/bin/python','-m','webapp.runtime_bootstrap'] and
            not images[profile['tag']]['secret_marker_findings'],
            'publication_state':'local_only','runtime_kind':profile['runtime_kind'],
            'grant_policy':'attempt grants must be a subset of model_routes plus authorized business capabilities'}
        for name,profile in spec['profiles'].items()}
    return {'schema_version':1,'generated_at':datetime.now(timezone.utc).isoformat(),
        'verification_scope':'targeted_only' if omitted else 'complete_image_set','omitted_profiles':omitted,
        'source':git_state(),'inputs':{'foundation':{**spec['foundation'],'image':foundation},
            'os_packages':os_package_lock(docker,'operation-runtime:build'),
            'wheels':wheels,'vendor':vendor},
        'skill_bundle':skills,'tool_schema_digest':tool_schema,
        'images':images,'profiles':profiles,'profile_dry_run':validation}


def host_capacity(max_resident=30,agent_share=.5,*,cpus=None,memory_bytes=None,disk_free_bytes=None):
    if not 1<=max_resident<=10000 or not 0<=agent_share<=1:raise ValueError('Invalid capacity inputs')
    cpus=cpus or (os.cpu_count() or 1)
    if memory_bytes is None:
        values={row.split(':',1)[0]:int(row.split()[1])*1024 for row in Path('/proc/meminfo').read_text().splitlines()}
        memory_bytes=values['MemTotal']
    if disk_free_bytes is None:disk_free_bytes=shutil.disk_usage(ROOT).free
    command={'cpu':1,'memory_bytes':1536*1024**2,'workspace_bytes':4*1024**3}
    agent={'cpu':2,'memory_bytes':4096*1024**2,'workspace_bytes':8*1024**3}
    per={key:math.ceil(command[key]*(1-agent_share)+agent[key]*agent_share) for key in command}
    budgets={'cpu':max(0,cpus-max(4,math.ceil(cpus*.15))),
        'memory_bytes':max(0,memory_bytes-max(8*1024**3,math.ceil(memory_bytes*.2))),
        'workspace_bytes':max(0,disk_free_bytes-math.ceil(disk_free_bytes*.15))}
    limits={'cpu':budgets['cpu']//per['cpu'],'memory':budgets['memory_bytes']//per['memory_bytes'],
        'disk':budgets['workspace_bytes']//per['workspace_bytes']}
    supported=min(max_resident,*limits.values())
    return {'requested_max_resident':max_resident,'agent_share':agent_share,
        'host':{'cpus':cpus,'memory_bytes':memory_bytes,'disk_free_bytes':disk_free_bytes},
        'reserve_policy':{'cpu':'max(4,15%)','memory':'max(8GiB,20%)','disk':'15% of current free'},
        'per_sandbox_weighted':per,'resource_budgets':budgets,'limits':limits,
        'supported_resident':supported,'requested_is_feasible':supported>=max_resident,
        'recommended_create_concurrency':max(1,min(4,cpus//16,max_resident))}


def write_json(path,value):
    path=Path(path);path.parent.mkdir(parents=True,exist_ok=True)
    path.write_text(json.dumps(value,indent=2,sort_keys=True)+'\n');return path


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('--docker',default=os.environ.get('DOCKER','docker'))
    parser.add_argument('--spec',type=Path,default=SPEC)
    parser.add_argument('--output',type=Path,default=DEFAULT_OUTPUT)
    parser.add_argument('--max-resident',type=int,default=30)
    parser.add_argument('--agent-share',type=float,default=.5)
    parser.add_argument('--freeze-bundle',action='store_true')
    parser.add_argument('--profile',action='append',help='Generate explicitly targeted evidence only for these Runtime Profiles')
    args=parser.parse_args()
    if args.freeze_bundle:
        spec=load_spec(args.spec);bundle=bundle_record(ROOT,spec['skill_bundle'])
        value={'schema_version':1,'skill_digest':bundle['digest'],
            'tool_schema_digest':'sha256:'+sha256(PUBLIC/'capabilities.json'),'files':bundle['files']}
        print(write_json(PUBLIC/'bundle.json',value));return
    value=manifest(args.docker,args.spec,profiles=args.profile)
    value['capacity']=host_capacity(args.max_resident,args.agent_share)
    value['capacity_scenarios']={name:host_capacity(args.max_resident,share)
        for name,share in (('all_command',0),('mixed_50_percent_agent',.5),('all_agent',1))}
    print(write_json(args.output,value))


if __name__=='__main__':main()
