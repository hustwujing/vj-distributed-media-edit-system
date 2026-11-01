"""Build a minimal OS sandbox; never mount the application database or secrets."""
from __future__ import annotations

import json
import os
import pwd
import re
import shutil
import stat
import subprocess
import uuid
from pathlib import Path
from .paths import SANDBOX_WORKSPACE

REPO = Path(__file__).resolve().parents[1]
PYTHON_PREFIX = Path('/data/wujing03/miniforge3')
CODEX_BIN = '/usr/local/lib/node_modules/@openai/codex/node_modules/@openai/codex-linux-x64/vendor/x86_64-unknown-linux-musl/bin/codex'


def _initialize_project_memory(path, uid, gid):
    """Bootstrap optional session memory before mounting a read-only plan workspace."""
    try:
        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    except FileExistsError:
        # The agent can edit this file; never overwrite it or follow a link as root.
        if not stat.S_ISREG(path.lstat().st_mode):
            raise RuntimeError('Project memory must be a regular file.')
        return
    with os.fdopen(descriptor, 'w', encoding='utf-8') as memory:
        os.fchown(memory.fileno(), uid, gid)
        memory.write('# 项目剪辑记录\n\n'
                     '此文件保存本项目已执行的剪辑策略、决策、验证结果和待办事项。'
                     '尚无 Session 章节时，表示暂无剪辑历史。\n')


def prepare(store, project_id, model, mode, turn_id=None, preview_id=None):
    from .task_leases import CURRENT
    lease = CURRENT.get()
    runtime = lease.runtime if lease else None
    if runtime and runtime.env is not None:
        return runtime.env
    account = pwd.getpwnam('video-use')
    uid, gid = (runtime.uid, runtime.gid) if runtime else (account.pw_uid, account.pw_gid)
    project = store.project_dir(project_id)
    agent = runtime.agent if runtime else project / 'agent'
    workspace, state = (runtime.workspace, runtime.state) if runtime else (agent/'workspace', agent/'state')
    turn_id=turn_id or uuid.uuid4().hex
    if len(turn_id)!=32 or any(c not in '0123456789abcdef' for c in turn_id):
        raise ValueError('Invalid preview turn identifier')
    review_scratch=preview_id is not None
    preview_id=preview_id or turn_id
    if not re.fullmatch('[a-f0-9]{32}',preview_id):raise ValueError('Invalid preview identifier')
    preview_root=runtime.preview_root if runtime else agent/'previews';preview=preview_root/preview_id
    for path in (agent, workspace, workspace/'edit', workspace/'edit/previews',workspace/'edit/transcripts', workspace/'sources', workspace/'transcripts', state, state/'codex', state/'cache',preview_root,preview):
        if path.is_symlink():
            raise RuntimeError('Agent workspace contains a symbolic directory; refusing to start.')
        path.mkdir(parents=True, exist_ok=True)
        if path != agent or not runtime:os.chown(path, uid, gid)
        path.chmod(0o700)
    _initialize_project_memory(workspace/'edit'/'project.md', uid, gid)
    assets = store.assets(project_id)
    manifest = []
    for asset in assets:
        manifest.append({'id':asset['id'],'name':asset['name'], **asset['metadata'],
                         'path':SANDBOX_WORKSPACE+'/sources/'+asset['filename'],
                         'transcripts':[SANDBOX_WORKSPACE+'/transcripts/'+p.name for p in
                                        (project/'edit'/'transcripts').glob(asset['id']+'*.json')
                                        if not p.is_symlink()]})
    from .conversation_history import write_snapshot
    history=agent/'conversation.jsonl'
    write_snapshot(store,project_id,turn_id,history);history.chmod(0o644)
    descriptor=os.open(workspace/'conversation.jsonl',os.O_WRONLY|os.O_CREAT|os.O_NOFOLLOW,0o644);os.close(descriptor)
    # Write trusted manifest atomically, avoiding any agent-created symlink.
    inventory = agent/'assets.json'
    inventory.write_text(json.dumps(manifest, ensure_ascii=False, indent=2))
    inventory.chmod(0o644)
    # Read-only canvas snapshot lets the Agent understand saved layer decisions.
    tables=store.query("SELECT name FROM sqlite_master WHERE type='table' AND name='image_documents'")
    documents=store.query('SELECT document FROM image_documents WHERE project=?',(project_id,)) if tables else []
    canvas=agent/'image-document.json'
    canvas.write_text(documents[0]['document'] if documents else '{}');canvas.chmod(0o644)
    fd=os.open(workspace/'image-document.json',os.O_WRONLY|os.O_CREAT|os.O_NOFOLLOW,0o644);os.close(fd)
    from . import workflows
    task = workflows.get(store, turn_id)
    workflow_state = agent/'workflow-state.json'
    workflow_state.write_text(json.dumps(workflows.public(task), ensure_ascii=False))
    workflow_state.chmod(0o644)
    fd=os.open(workspace/'workflow-state.json',os.O_WRONLY|os.O_CREAT|os.O_NOFOLLOW,0o644);os.close(fd)

    placeholder = workspace/'assets.json'
    if placeholder.is_symlink():
        raise RuntimeError('Asset manifest cannot be a symbolic link.')
    descriptor = os.open(placeholder, os.O_WRONLY | os.O_CREAT | os.O_NOFOLLOW, 0o644)
    os.close(descriptor)
    config = agent/'config.toml'
    config.write_text(f'''model = {json.dumps(model)}
model_provider = "video_use"
approval_policy = "never"
sandbox_mode = "danger-full-access"
model_reasoning_effort = "medium"
web_search = "disabled"

[model_providers.video_use]
name = "Video Use isolated gateway"
base_url = "http://127.0.0.1:18766"
wire_api = "responses"
requires_openai_auth = false
request_max_retries = 1
stream_max_retries = 1

[features]
multi_agent = false

[shell_environment_policy]
inherit = "all"
''')
    config.chmod(0o644)
    descriptor=os.open(workspace/'capabilities.json',os.O_WRONLY|os.O_CREAT|os.O_NOFOLLOW,0o644);os.close(descriptor)
    # Minimal passwd/group files contain only the sandbox service identity.
    (agent/'passwd').write_text(f'video-use:x:{uid}:{gid}:Video Use:/state:/bin/bash\n')
    (agent/'group').write_text(f'video-use:x:{gid}:\n')
    for name in ('passwd','group'):
        (agent/name).chmod(0o644)
    result = {'project':project, 'agent':agent, 'workspace':workspace, 'state':state,
            'preview_root':preview_root,'preview':preview,'preview_path':SANDBOX_WORKSPACE+'/edit/previews/'+preview_id,
            'uid':uid, 'gid':gid, 'assets':assets, 'mode':mode, 'runtime':runtime,
            'service_uid':account.pw_uid, 'service_gid':account.pw_gid,
            'sources':runtime.sources if runtime else project/'sources',
            'transcripts':runtime.transcripts if runtime else project/'edit/transcripts'}
    if review_scratch:
        for folder in (workspace/'edit/repairs',workspace/'edit/repairs'/preview_id):
            if folder.is_symlink():raise ValueError('Invalid repair directory')
            folder.mkdir(exist_ok=True,mode=0o700);os.chown(folder,uid,gid)
        result['repair_dir']=workspace/'edit/repairs'/preview_id
        result['repair_path']=SANDBOX_WORKSPACE+'/edit/repairs/'+preview_id
    from .operations import catalog
    from .execution_contracts import profile
    if lease and lease.kind=='manual':permission=profile(mode,fixed=True)
    elif lease and lease.kind=='workflow':
        step=next((s for s in task['steps'] if s['id']==lease.step_id),None) if task else None
        permission=profile(mode,step=True,fixed=bool(step and step['spec']['kind']!='script'))
    else:permission=profile(mode,review=review_scratch)
    result['permission_profile']=str(permission)
    if lease:store.execute("UPDATE execution_attempts SET permission_profile=? WHERE id=? AND permission_profile=''",(str(permission),lease.attempt))
    capabilities=json.loads((REPO/'runtime/public/capabilities.json').read_text())
    (agent/'capabilities.json').write_text(json.dumps({**capabilities,**catalog()},ensure_ascii=False))
    (agent/'capabilities.json').chmod(0o644)
    if runtime:runtime.env = result
    return result


def command(env, gateway_socket, executable=None):
    """Only this trusted function constructs mount paths and executable arguments."""
    project, agent = env['project'], env['agent']
    from .execution_contracts import profile
    permission=profile(env['mode'],review=bool(env.get('repair_dir')),step=bool(env.get('step_output')),fixed=bool(env.get('image_action')))
    from .task_leases import CURRENT
    lease=CURRENT.get()
    if lease:lease.store.execute("UPDATE execution_attempts SET permission_profile=? WHERE id=? AND permission_profile=''",(str(permission),lease.attempt))
    args = ['/usr/bin/bwrap', '--die-with-parent', '--new-session', '--unshare-pid', '--unshare-net',
            '--unshare-ipc', '--unshare-uts', '--unshare-cgroup-try', '--hostname', 'video-use-project']
    for directory in ('/opt','/opt/video-use','/opt/video-use/helpers','/gateway','/etc'):
        args += ['--perms','0755','--dir',directory]
    from .runtime_image import mounts
    args += mounts()
    args += ['--proc', '/proc', '--dev', '/dev',
            '--perms', '1777', '--size', '268435456', '--tmpfs', '/tmp',
            '--ro-bind', str(agent/'passwd'), '/etc/passwd',
            '--ro-bind', str(agent/'group'), '/etc/group']
    args += ['--ro-bind', str(REPO/'runtime/public/SKILL.md'), '/opt/video-use/SKILL.md',
             '--ro-bind', str(agent/'capabilities.json'),SANDBOX_WORKSPACE+'/capabilities.json',
             '--ro-bind', str(REPO/'references'), '/opt/video-use/references',
             '--ro-bind', str(REPO/'webapp/sandbox_entry.py'), '/opt/sandbox_entry.py',
             '--ro-bind', str(REPO/'webapp/sandbox_limits.py'), '/opt/sandbox_limits.py',
             '--ro-bind', str(REPO/'webapp/canvas_pixels.py'), '/opt/canvas_pixels.py',
             '--ro-bind', str(REPO/'webapp/agent_transcribe.py'), '/opt/transcribe.py',
             '--ro-bind', str(REPO/'webapp/agent_image.py'), '/opt/image_ai.py',
             '--ro-bind', str(REPO/'webapp/agent_canvas.py'), '/opt/canvas_edit.py',
             '--ro-bind', str(REPO/'webapp/agent_workflow.py'), '/opt/workflow.py',
             '--ro-bind', str(REPO/'webapp/workflow_io.py'), '/opt/workflow_io.py',
             '--ro-bind', str(REPO/'webapp/operation_io.py'), '/opt/operation_io.py',
             '--ro-bind', str(REPO/'webapp/script_preflight.py'), '/opt/script_preflight.py',
             '--ro-bind', str(REPO/'webapp/step_sdk.py'), '/opt/step.py',
             '--ro-bind', str(REPO/'webapp/preview_export.py'), '/opt/preview_export.py',
             '--ro-bind', str(REPO/'webapp/validate_outputs.py'), '/opt/validate_outputs.py',
             '--ro-bind', str(REPO/'webapp/quality_check.py'), '/opt/quality_check.py',
             '--bind', str(env['state']), '/state',
             '--ro-bind', str(agent/'config.toml'), '/state/codex/config.toml',
             '--bind' if env['mode']=='execute' else '--ro-bind', str(env['workspace']), SANDBOX_WORKSPACE,
             '--ro-bind', str(env['preview_root']), SANDBOX_WORKSPACE+'/edit/previews',
             '--bind', str(env['preview']), env['preview_path'],
             '--ro-bind', str(env['sources']), SANDBOX_WORKSPACE+'/sources',
             '--ro-bind', str(agent/'assets.json'), SANDBOX_WORKSPACE+'/assets.json',
             '--ro-bind', str(agent/'image-document.json'), SANDBOX_WORKSPACE+'/image-document.json',
             '--ro-bind', str(agent/'workflow-state.json'), SANDBOX_WORKSPACE+'/workflow-state.json',
             '--ro-bind', str(agent/'conversation.jsonl'), SANDBOX_WORKSPACE+'/conversation.jsonl',
             '--ro-bind', str(env['transcripts']), SANDBOX_WORKSPACE+'/transcripts',
             # Keep old saved scripts/conversations usable after the path migration.
             '--symlink', SANDBOX_WORKSPACE+'/transcripts', '/transcripts',
             '--ro-bind', str(gateway_socket), '/gateway/model.sock',
             '--chdir', SANDBOX_WORKSPACE, '--clearenv',
             '--setenv', 'PATH', '/opt/venv/bin:/usr/bin:/bin',
             '--setenv', 'HOME', '/state', '--setenv', 'CODEX_HOME', '/state/codex',
             '--setenv', 'XDG_CACHE_HOME', '/state/cache',
             '--setenv', 'MPLCONFIGDIR', '/state/cache/matplotlib',
             '--setenv', 'TMPDIR', '/tmp', '--setenv', 'LANG', 'C.UTF-8',
             '--setenv', 'PYTHONDONTWRITEBYTECODE', '1',
             '--setenv', 'PYTHONHOME', '/opt/python',
             '--setenv', 'LD_LIBRARY_PATH', '/opt/python/lib',
             '--setenv', 'PYTHONPYCACHEPREFIX', '/tmp/pycache',
             '--setenv', 'PYTHONPATH', '/opt',
             '--setenv', 'OPENBLAS_NUM_THREADS', '2', '--setenv', 'OMP_NUM_THREADS', '2',
             '--cap-add', 'CAP_SETUID', '--cap-add', 'CAP_SETGID',
             '/usr/bin/setpriv', '--reuid='+str(env['uid']), '--regid='+str(env['gid']),
             '--groups='+str(env['runtime'].read_gid) if env.get('runtime') else '--clear-groups',
             '--no-new-privs', '--bounding-set=-all']
    capabilities=json.loads((REPO/'runtime/public/capabilities.json').read_text())
    if env.get('repair_dir'):
        position=args.index('--chdir')
        args[position:position]=['--bind',str(env['repair_dir']),env['repair_path']]
    if gateway_socket is None:
        index=args.index('/gateway/model.sock')
        del args[index-2:index+1]
    position=args.index('--chdir')
    legacy=not env.get('runtime') or getattr(env['runtime'],'legacy_paths',False)
    if legacy:
        old=REPO/'workspace'
        aliases=[]
        for parent in reversed(old.parents):
            if str(parent)!='/':aliases+=['--perms','0755','--dir',str(parent)]
        aliases+=['--symlink',SANDBOX_WORKSPACE,str(old)]
        args[position:position]=aliases
        position+=len(aliases)
    for name in capabilities['helpers']:
        args[position:position]=['--ro-bind',str(REPO/'helpers'/name),'/opt/video-use/helpers/'+name]
        position+=3
    if env.get('step_output'):
        position = args.index('--chdir')
        args[position:position] = ['--bind', str(env['workspace']/env['step_output']),
                                   SANDBOX_WORKSPACE+'/'+env['step_output'],
                                   '--perms','1777','--size','67108864','--tmpfs','/state']
    if env.get('manual'):
        from .runtime_image import MANIFEST
        position=args.index('--chdir')
        args[position:position]=['--ro-bind',str(MANIFEST.parent/'tool-app'),'/opt/app',
                                  '--ro-bind',str(env['transcripts']),SANDBOX_WORKSPACE+'/edit/transcripts']
        position=args.index('--cap-add')
        args[position:position]=['--setenv','PYTHONPATH','/opt/app',
                                 '--setenv','VIDEO_USE_ISOLATED','1']
    args += ['/opt/venv/bin/python', '/opt/sandbox_limits.py']
    args += executable or ['/opt/venv/bin/python','/opt/sandbox_entry.py','/opt/codex']
    return env['runtime'].wrap(args,env.get('resource_scope')) if env.get('runtime') else args


ARTIFACT_EXTENSIONS={'.mp4','.webm','.mov','.mkv','.png','.jpg','.jpeg','.webp','.srt','.ass','.json','.txt','.md','.wav','.mp3','.m4a','.flac','.ogg','.aac','.opus','.py','.psd','.ora','.zip','.tiff'}


def output_fingerprint(info):
    return (info.st_dev,info.st_ino,info.st_size,info.st_mtime_ns,info.st_ctime_ns)


def snapshot_outputs(env):
    base=env['workspace']/'edit'
    if base.is_symlink():raise RuntimeError('Output directory cannot be a symbolic link.')
    files={}
    for directory,dirs,names,fd in os.fwalk(base,follow_symlinks=False):
        dirs[:]=sorted(name for name in dirs if not name.startswith('.'))
        for name in names:
            if name.startswith('.') or Path(name).suffix.lower() not in ARTIFACT_EXTENSIONS:continue
            try:info=os.stat(name,dir_fd=fd,follow_symlinks=False)
            except OSError:continue
            if stat.S_ISREG(info.st_mode):files[str((Path(directory)/name).relative_to(base))]=output_fingerprint(info)
    return files


def delivery_manifest(base,baseline,current):
    # A stale declaration from a previous turn must not pin an old result.
    if 'deliverables.json' not in current or (baseline is not None and current['deliverables.json']==baseline.get('deliverables.json')):
        return {},set()
    try:
        fd=os.open(base/'deliverables.json',os.O_RDONLY|os.O_NOFOLLOW|os.O_NONBLOCK)
        with os.fdopen(fd,'rb') as stream:
            info=os.fstat(stream.fileno())
            if not stat.S_ISREG(info.st_mode) or not 0<info.st_size<=65536:return {},set()
            declaration=json.load(stream)
        if not isinstance(declaration,dict):return {},set()
        names=declaration.get('files',[])
        if not isinstance(names,list) or len(names)>40:return {},set()
        primary=declaration.get('primary')
        if not isinstance(primary,str):return {},set()
        selected={name for name in [primary,*names] if isinstance(name,str) and name in current
                  and not Path(name).is_absolute() and '..' not in Path(name).parts}
        return declaration,selected
    except (OSError,ValueError,TypeError):return {},set()


def validate_generated(env,names):
    if env.get('runtime') and names:
        import socket
        import tempfile
        runtime=env['runtime']
        runtime.lease.check()
        socket_path=runtime.path/(uuid.uuid4().hex[:12]+'.sock')
        try:
            with socket.socket(socket.AF_UNIX) as sock, tempfile.TemporaryFile() as output_check, tempfile.TemporaryFile() as errors:
                sock.bind(str(socket_path));os.chown(socket_path,env['uid'],env['gid']);socket_path.chmod(0o600)
                result=subprocess.run(command(dict(env,mode='plan'),socket_path,
                    ['/opt/venv/bin/python','/opt/validate_outputs.py']),
                    input=json.dumps(names[:40]).encode(),
                    stdout=output_check,stderr=errors,timeout=120)
                if result.returncode:raise RuntimeError('生成文件未通过格式检查，未发布本轮结果。')
        finally:socket_path.unlink(missing_ok=True)


def export_results(env,turn_id,text=''):
    """Publish changed outputs and explicit deliveries after the Agent exits."""
    import hashlib
    import mimetypes
    from .artifacts import order_artifacts
    base=env['workspace']/'edit'
    current=snapshot_outputs(env);baseline=env.get('output_baseline')
    declaration,selected=delivery_manifest(base,baseline,current)
    candidates=[]
    for name,fingerprint in current.items():
        if baseline is not None and fingerprint==baseline.get(name) and name not in selected:continue
        if not 0<fingerprint[2]<=2*1024**3:continue
        candidates.append({'name':name,'type':mimetypes.guess_type(name)[0] or 'text/plain',
                           'source_modified':fingerprint[3]/1e9,'role':'deliverable' if name in selected else 'output'})
    candidates=order_artifacts(candidates,text,declaration.get('primary'))
    validate_generated(env,[item['name'] for item in candidates[:40]])
    output=env['project']/'edit'/'agent_exports'/turn_id
    output.mkdir(parents=True,exist_ok=True);os.chown(output,env.get('service_uid',env['uid']),env.get('service_gid',env['gid']));output.chmod(0o750)
    artifacts=[];total=0
    for item in candidates:
        source=base/item['name']
        if not source.resolve().is_relative_to(base.resolve()):continue
        try:fd=os.open(source,os.O_RDONLY|os.O_NOFOLLOW|os.O_NONBLOCK)
        except OSError:continue
        with os.fdopen(fd,'rb') as stream:
            info=os.fstat(stream.fileno())
            if not stat.S_ISREG(info.st_mode) or not 0<info.st_size<=2*1024**3:continue
            if len(artifacts)>=40 or total+info.st_size>4*1024**3:continue
            filename=uuid.uuid4().hex+source.suffix.lower();destination=output/filename
            digest=hashlib.sha256()
            from .egress_policy import copy_public
            with destination.open('xb') as target:
                if env.get('runtime'):
                    runtime=env['runtime'];runtime.exports=getattr(runtime,'exports',[])+[destination]
                copy_public(stream,target,digest)
            destination.chmod(0o640);os.chown(destination,env.get('service_uid',env['uid']),env.get('service_gid',env['gid']));total+=info.st_size
            artifacts.append({**item,'file':filename,'size':info.st_size,'sha256':digest.hexdigest(),
                              'url':f'api/projects/{env["project"].name}/agent/{turn_id}/files/{filename}'})
            if env.get('runtime') and runtime.store.shared_files:
                artifacts[-1]['artifact_id']=runtime.store.artifacts.save_file(env['project'].name,str(destination.relative_to(env['project'])))['id']
    return artifacts


def export_previews(env,turn_id,gateway_socket):
    """Inspect after the Agent exits, then copy only this turn's regular images."""
    if not any(env['preview'].iterdir()):return []
    inspected=subprocess.run(command(env,gateway_socket,[
        '/opt/venv/bin/python','/opt/preview_export.py',env['preview_path']]),
        capture_output=True,text=True,timeout=45,env={'PATH':'/usr/bin:/bin','LANG':'C.UTF-8'})
    if inspected.returncode:raise RuntimeError('样式预览图片检查失败：'+inspected.stderr[-1000:])
    records=json.loads(inspected.stdout)
    output=env['project']/'edit'/'agent_exports'/turn_id
    artifacts=[];total=0
    for record in records[:12]:
        relative=Path(record['name'])
        if relative.is_absolute() or '..' in relative.parts:continue
        if relative.suffix.lower() not in {'.png','.jpg','.jpeg','.webp'}:continue
        source=env['preview']/relative
        if not source.resolve().is_relative_to(env['preview'].resolve()):continue
        try:fd=os.open(source,os.O_RDONLY|os.O_NOFOLLOW|os.O_NONBLOCK)
        except OSError:continue
        with os.fdopen(fd,'rb') as stream:
            info=os.fstat(stream.fileno())
            if not stat.S_ISREG(info.st_mode) or info.st_size!=record['size'] or not 0<info.st_size<=32*1024**2:continue
            if total+info.st_size>128*1024**2:break
            output.mkdir(parents=True,exist_ok=True);os.chown(output,env.get('service_uid',env['uid']),env.get('service_gid',env['gid']));output.chmod(0o750)
            extension=source.suffix.lower();filename=uuid.uuid4().hex+extension
            destination=output/filename
            with destination.open('xb') as target:
                if env.get('runtime'):
                    runtime=env['runtime'];runtime.exports=getattr(runtime,'exports',[])+[destination]
                from .egress_policy import copy_public
                copy_public(stream,target)
            os.chown(destination,env.get('service_uid',env['uid']),env.get('service_gid',env['gid']));destination.chmod(0o640)
            total+=info.st_size
            if env.get('runtime') and runtime.store.shared_files:
                runtime.store.artifacts.save_file(env['project'].name,str(destination.relative_to(env['project'])))
            mime={'.png':'image/png','.jpg':'image/jpeg','.jpeg':'image/jpeg','.webp':'image/webp'}[extension]
            artifacts.append({'name':relative.as_posix(),
                              'file':filename,'size':info.st_size,'width':record['width'],'height':record['height'],
                              'type':mime,'purpose':'style_preview',
                              'url':f'api/projects/{env["project"].name}/agent/{turn_id}/files/{filename}'})
    return artifacts
