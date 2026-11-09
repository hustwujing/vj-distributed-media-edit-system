#!/usr/bin/env python3
"""Verify advertised CPU code-editor dependencies in bwrap and an OCI image."""
import argparse
import json
from pathlib import Path
import shutil
import subprocess
import sys

from webapp.agent_runtime_capabilities import catalogue


def local(capabilities):
    modules={name:subprocess.run([sys.executable,'-c','import '+name],capture_output=True).returncode==0
             for name in capabilities['python_modules']}
    tools={name:bool(shutil.which(name)) for name in capabilities['system_tools']}
    return {'python_modules':modules,'system_tools':tools}


def image(docker,reference,capabilities):
    script=('import importlib.util,json,shutil; print(json.dumps({"python_modules":{name:bool(importlib.util.find_spec(name)) '
        'for name in '+repr(capabilities['python_modules'])+'},"system_tools":{name:bool(shutil.which(name)) for name in '
        +repr(capabilities['system_tools'])+'}}))')
    output=subprocess.check_output([docker,'run','--rm','--network','none','--read-only','--cap-drop','ALL',
        '--security-opt','no-new-privileges','--entrypoint','/opt/venv/bin/python',reference,'-c',script],text=True,timeout=120)
    return json.loads(output)


def main():
    parser=argparse.ArgumentParser();parser.add_argument('--docker',default='docker');parser.add_argument('--image',required=True)
    parser.add_argument('--output',type=Path,required=True);args=parser.parse_args()
    catalog=catalogue();host=local(catalog['bwrap_cpu']);sandbox=image(args.docker,args.image,catalog['opensandbox_cpu'])
    missing_host={kind:sorted(name for name,ok in values.items() if not ok) for kind,values in host.items()}
    missing_sandbox={kind:sorted(name for name,ok in values.items() if not ok) for kind,values in sandbox.items()}
    result={'passed':not any((*missing_host.values(),*missing_sandbox.values())),
        'scope':'advertised non-GPU code-editor capabilities; GPU models and arbitrary package installation excluded',
        'bwrap_cpu':host,'opensandbox_cpu':sandbox,'missing_bwrap':missing_host,'missing_opensandbox':missing_sandbox}
    args.output.parent.mkdir(parents=True,exist_ok=True);args.output.write_text(json.dumps(result,indent=2)+'\n')
    print(json.dumps({'passed':result['passed'],'missing_bwrap':missing_host,'missing_opensandbox':missing_sandbox}))
    if not result['passed']:raise SystemExit(1)


if __name__=='__main__':main()
