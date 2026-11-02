"""Build the approved executable/library manifest; never run on user inputs."""
import concurrent.futures
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys

REPO=Path(__file__).resolve().parents[1]
MANIFEST=Path(os.environ.get('VIDEO_USE_RUNTIME_BUILD',str(REPO/'runtime/build')))/'manifest.json'
TOOLS=('bash','sh','env','cat','ls','find','sed','awk','sort','uniq','wc','head','tail','cut',
       'date','mkdir','cp','mv','rm','touch','printf','timeout','nice','ps','kill','pwd',
       'basename','dirname','readlink','realpath','stat','du','df','grep','xargs','tee',
       'ffmpeg','ffprobe','socat','setpriv','ldconfig','fc-match','fc-list','file','rg','uname','sleep','tr','true','false')
CODEX_DEFAULT='/usr/local/lib/node_modules/@openai/codex/node_modules/@openai/codex-linux-x64/vendor/x86_64-unknown-linux-musl/bin/codex'
TOOL_MODULES=('__init__.py','telemetry.py','api_capacity.py','worker.py','media_handlers.py','operation_result.py','image_tasks_cli.py','image_tasks.py','image_document.py',
              'image_providers.py','image_models.py','image_replace.py','sam3_zim.py','models.py','edit_tools.py','quality_review.py')


def stage_tools(directory):
    """Build a versioned tool package; callers choose a test or release directory."""
    tool_app=Path(directory)/'tool-app'
    for folder in (tool_app,tool_app/'webapp',tool_app/'helpers'):
        folder.mkdir(parents=True,exist_ok=True);folder.chmod(0o755)
    for name in TOOL_MODULES:
        shutil.copyfile(REPO/'webapp'/name,tool_app/'webapp'/name)
        (tool_app/'webapp'/name).chmod(0o644)
    for name in json.loads((REPO/'runtime/public/capabilities.json').read_text())['helpers']:
        shutil.copyfile(REPO/'helpers'/name,tool_app/'helpers'/name)
        (tool_app/'helpers'/name).chmod(0o644)
    shutil.copyfile(REPO/'runtime/public/transcribe.py',tool_app/'helpers/transcribe.py')
    (tool_app/'helpers/transcribe.py').chmod(0o644)
    if not (tool_app/'.venv').is_symlink():(tool_app/'.venv').symlink_to('/opt/venv')
    digest=hashlib.sha256()
    for path in sorted([*(tool_app/'webapp').glob('*.py'),*(tool_app/'helpers').glob('*.py')]):
        digest.update(str(path.relative_to(tool_app)).encode());digest.update(path.read_bytes())
    (tool_app/'package.sha256').write_text(digest.hexdigest())
    return tool_app


def fingerprint():
    if not MANIFEST.is_file():return 'unbuilt'
    manifest=json.loads(MANIFEST.read_text())
    package=MANIFEST.parent/'tool-app/package.sha256'
    value={key:manifest.get(key) for key in ('version','python_version','packages','harnesses')}
    value['tools']=package.read_text().strip() if package.is_file() else ''
    value['harness_bridge']={name:hashlib.sha256((REPO/'webapp'/name).read_bytes()).hexdigest()
        for name in ('harness_adapters.py','harness_bridge.py','harness_sdk_events.py')}
    return hashlib.sha256(json.dumps(value,sort_keys=True).encode()).hexdigest()


def build():
    prefix=Path(sys.base_prefix)
    codex=Path(os.environ.get('VIDEO_USE_CODEX_BIN',CODEX_DEFAULT)).resolve()
    executables={name:Path(shutil.which(name) or '/missing/'+name).resolve() for name in TOOLS}
    missing=[name for name,path in executables.items() if not path.is_file()]
    if missing:raise RuntimeError('Required runtime tools missing: '+','.join(missing))
    if not codex.is_file():raise RuntimeError('Codex runtime is not installed')
    libraries={}
    cache=subprocess.check_output(['ldconfig','-p'],text=True)
    match=re.search(r'libseccomp\.so\.2[^\n]*=>\s+(\S+)',cache)
    if not match:raise RuntimeError('libseccomp is required')
    libraries[match[1]]=str(Path(match[1]).resolve())
    code_host=codex.with_name('codex-code-mode-host')
    native=set(executables.values())|{Path(sys.executable).resolve(),codex}
    harnesses={'codex':{'sha256':hashlib.sha256(codex.read_bytes()).hexdigest()}}
    # Only installed, pinned SDK distributions contribute native dependencies.
    # Their executables stay under the existing read-only venv mount.
    from importlib.util import find_spec
    from importlib.metadata import version
    for package,binary in (('claude_agent_sdk','_bundled/claude'),('deepseek_harness_runtime',None)):
        found=find_spec(package)
        if found:
            if binary:path=Path(found.origin).parent/binary
            else:
                from deepseek_harness_runtime import bundled_runtime_path
                path=bundled_runtime_path()
            if not path.is_file():raise RuntimeError('Installed harness executable is unavailable: '+package)
            native.add(path)
            harnesses[package]={'version':version('claude-agent-sdk' if binary else 'deepseek-harness-runtime-bin'),
                                'sha256':hashlib.sha256(path.read_bytes()).hexdigest()}
    if code_host.is_file():native.add(code_host)
    native.update((REPO/'.venv/lib').rglob('*.so*'))
    for name in ('reference_video_remix_ocr','sam3'):
        model_env=Path('/data/wujing03/miniforge3/envs')/name
        native.update(model_env.glob('lib/python*/site-packages/cv2/*.so'))
    native.update((prefix/'lib'/f'python{sys.version_info.major}.{sys.version_info.minor}'/'lib-dynload').glob('*.so'))
    def dependencies(path):
        if not path.is_file():return []
        result=subprocess.run(['ldd',str(path)],capture_output=True,text=True,timeout=15)
        return re.findall(r'(?:=>\s+|^\s*)(/[^\s()]+)',result.stdout,re.M)
    with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
        for paths in pool.map(dependencies,sorted(native)):
            for value in paths:
                path=Path(value)
                if not path.is_file():continue
                # Application packages are already an approved read-only mount.
                if path.is_relative_to(REPO/'.venv'):continue
                resolved=path.resolve()
                libraries[str(path)]=str(resolved)
                libraries[str(resolved)]=str(resolved)
    from importlib.metadata import distributions
    packages=sorted((d.metadata.get('Name',''),d.version) for d in distributions())
    result={'packages':packages,'harnesses':harnesses,'version':2,'python':str(Path(sys.executable).resolve()),'prefix':str(prefix),
        'python_version':f'{sys.version_info.major}.{sys.version_info.minor}',
        'codex':str(codex),'code_host':str(code_host) if code_host.is_file() else None,'tools':{k:str(v) for k,v in executables.items()},'libraries':libraries}
    MANIFEST.parent.mkdir(parents=True,exist_ok=True)
    tool_app=MANIFEST.parent/'tool-app'
    for directory in (tool_app,tool_app/'webapp',tool_app/'helpers'):
        directory.mkdir(exist_ok=True);directory.chmod(0o755)
    for name in TOOL_MODULES:
        shutil.copyfile(REPO/'webapp'/name,tool_app/'webapp'/name)
        (tool_app/'webapp'/name).chmod(0o644)
    for name in json.loads((REPO/'runtime/public/capabilities.json').read_text())['helpers']:
        shutil.copyfile(REPO/'helpers'/name,tool_app/'helpers'/name)
        (tool_app/'helpers'/name).chmod(0o644)
    shutil.copyfile(REPO/'runtime/public/transcribe.py',tool_app/'helpers/transcribe.py')
    (tool_app/'helpers/transcribe.py').chmod(0o644)
    if not (tool_app/'.venv').is_symlink():(tool_app/'.venv').symlink_to('/opt/venv')
    stage_tools(MANIFEST.parent)
    MANIFEST.write_text(json.dumps(result,indent=2));MANIFEST.chmod(0o644)
    MANIFEST.parent.chmod(0o755)
    print('Approved runtime manifest built:',len(executables),'tools,',len(libraries),'library paths')


def mounts():
    if not MANIFEST.is_file():raise RuntimeError('Run deploy/prepare-executor.sh to build the approved runtime')
    data=json.loads(MANIFEST.read_text())
    args=['--dir','/usr/bin','--dir','/usr/sbin','--symlink','/usr/bin','/bin','--symlink','/usr/sbin','/sbin']
    for name,source in data['tools'].items():
        target='/usr/sbin/ldconfig' if name=='ldconfig' else '/usr/bin/'+name
        args+=['--ro-bind',source,target]
    for target,source in data['libraries'].items():
        if target.startswith(data['prefix']+'/lib/'):
            target='/opt/python/lib/'+target[len(data['prefix']+'/lib/'):]
        args+=['--ro-bind',source,target]
    args+=['--ro-bind',data['python'],'/opt/python/bin/python',
           '--ro-bind',data['python'],'/opt/venv/bin/python',
           '--symlink','python','/opt/venv/bin/python3',
           '--ro-bind',str(REPO/'.venv/lib'),'/opt/venv/lib',
           '--ro-bind',str(Path(data['prefix'])/'lib'/('python'+data['python_version'])),'/opt/python/lib/python'+data['python_version'],
           '--tmpfs','/opt/python/lib/python'+data['python_version']+'/site-packages',
           '--ro-bind',data['codex'],'/opt/codex',
           '--ro-bind',str(REPO/'runtime/public/pyvenv.cfg'),'/opt/venv/pyvenv.cfg',
           '--setenv','PYTHONHOME','/opt/python',
           '--setenv','PYTHONPYCACHEPREFIX','/tmp/pycache']
    for directory in ('/usr/share/fonts','/usr/share/fontconfig','/usr/share/zoneinfo','/usr/share/terminfo','/etc/fonts','/etc/ssl/certs'):
        if Path(directory).exists():args+=['--ro-bind',directory,directory]
    if data.get('code_host'):args+=['--ro-bind',data['code_host'],'/opt/codex-code-mode-host']
    # Hide editable-install source URLs without changing package/version metadata.
    for path in (REPO/'.venv/lib').glob('python*/site-packages/*.dist-info/direct_url.json'):
        args+=['--ro-bind',str(REPO/'runtime/public/origin.json'),'/opt/venv/lib/'+str(path.relative_to(REPO/'.venv/lib'))]
    parents=set()
    for i,value in enumerate(args):
        if value=='--ro-bind':parents.update(p for p in Path(args[i+2]).parents if str(p)!='/')
    directories=[]
    for path in sorted(parents,key=lambda p:(len(p.parts),str(p))):
        directories+=['--perms','0755','--dir',str(path)]
    return directories+args


if __name__=='__main__':build()
