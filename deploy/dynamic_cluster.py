"""Isolated dynamic-plan deployment. Never operates main's services or database."""
from __future__ import annotations
import argparse
import base64
import fcntl
import hashlib
import json
import os
from pathlib import Path
import pwd
import secrets
import shutil
import subprocess
import sys
import time

REPO=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(REPO))
from deploy import common
from deploy import systemd_templates

NAMESPACE=os.environ.get('DAS_DEPLOY_NAMESPACE','dynamic-agent')
ROOT=Path(os.environ.get('DAS_DEPLOY_ROOT',str(REPO/'.cluster')))
CONF=Path(os.environ.get('DAS_DEPLOY_CONF','/etc/distributed-agent-system'))
EXECUTORS=Path(os.environ.get('DAS_DEPLOY_EXECUTORS',str(REPO.parent/'da-exec')))
PORTS={'a':int(os.environ.get('DAS_DEPLOY_API_A_PORT','19766')),
       'b':int(os.environ.get('DAS_DEPLOY_API_B_PORT','19767'))}
UIDS={'a':int(os.environ.get('DAS_DEPLOY_UID_A','710000')),
      'b':int(os.environ.get('DAS_DEPLOY_UID_B','710100'))}
ENTRY=os.environ.get('DAS_DEPLOY_ENTRY','/distributed-agent-system')
DATABASE=os.environ.get('DAS_DEPLOY_DATABASE','distributed_agent_system')
ROLE=os.environ.get('DAS_DEPLOY_ROLE','distributed_agent_system')
ORCHESTRATOR_ROLE=os.environ.get('DAS_DEPLOY_ORCHESTRATOR_ROLE','distributed_agent_orchestrator')
OBJECT_PREFIX=os.environ.get('DAS_DEPLOY_OBJECT_PREFIX','distributed-agent-system')
STREAM_PREFIX=os.environ.get('DAS_DEPLOY_STREAM_PREFIX','distributed-agent-system:events:')
OBS_PORT=int(os.environ.get('DAS_DEPLOY_OBS_PORT','19768'))
PROXY_PORT=int(os.environ.get('DAS_DEPLOY_PROXY_PORT','19765'))
GPU_LEASE_ROOT=Path(os.environ.get('DAS_DEPLOY_GPU_LEASE_ROOT','/data/wujing03/.media-gpu-leases'))
CONTROL_LOCK=Path(os.environ.get('DAS_DEPLOY_CONTROL_LOCK','/run/distributed-agent-system-control.lock'))
MEDIA_RUNTIME_PREFIX=os.environ.get('DAS_DEPLOY_MEDIA_RUNTIME_PREFIX','da-media-')
PG_SOCKET='/run/video-use-db'
PG_PORT=15432
PG_BIN=Path('/usr/lib/postgresql/16/bin')
APIS=[f'{NAMESPACE}-api@{n}.service' for n in PORTS]
WORKERS=[f'{NAMESPACE}-executor@{n}.service' for n in PORTS]
REAPERS=[f'{NAMESPACE}-watchdog@{n}.service' for n in PORTS]
MEDIA=[f'{NAMESPACE}-media@{n}.service' for n in PORTS]
ORCHESTRATOR=NAMESPACE+'-orchestrator.service'
OBSERVER=NAMESPACE+'-observer.service'
SERVICES=APIS+WORKERS+REAPERS+MEDIA+[ORCHESTRATOR,OBSERVER]
CAPACITY_ENV={'VIDEO_USE_CONCURRENCY':'4','VIDEO_USE_NODE_CONCURRENCY':'4','VIDEO_USE_USER_CONCURRENCY':'8',
              'VIDEO_USE_NODE_CPU_SLOTS':'12','VIDEO_USE_NODE_MEMORY_MB':'49152'}
run=common.run
env_file=common.env_file
private_env=common.private_env


def active(name):return common.active(name,runner=run)
def parallel_service(action,names):return common.parallel_service(action,names,runner=run)


def data(role,node):return ROOT/(role+'-'+node)
def registry(node):return EXECUTORS/node
def release():return Path(json.loads((ROOT/'release.json').read_text())['source'])


def environment(node='a',source=None):
    values=dict(os.environ)
    for name in ('.env','.agent.env','.storage.env','.distributed.env'):values.update(env_file(REPO/name))
    values.update(env_file(CONF/f'node-{node}.env'));values.update(env_file(CONF/'observation.env'))
    values['VIDEO_USE_RUNTIME_BUILD']=str((source or release())/'runtime/build')
    return values


def validate_isolation(values):
    from urllib.parse import urlsplit
    parsed=urlsplit(values['VIDEO_USE_DATABASE_URL'])
    if parsed.path!='/'+DATABASE or parsed.username!=ROLE:raise ValueError('Dedicated database and role required')
    if values.get('VIDEO_USE_DEPLOYMENT')!=NAMESPACE:raise ValueError('Wrong deployment namespace')
    if values.get('VIDEO_USE_OBJECT_PREFIX')!=OBJECT_PREFIX:raise ValueError('Dedicated BOSS prefix required')
    if not values.get('VIDEO_USE_AGENT_STREAM_PREFIX','').startswith(STREAM_PREFIX):
        raise ValueError('Dedicated Redis prefix required')
    for path in (ROOT,EXECUTORS,CONF):
        forbidden={'/data2/wujing03/video-use/.cluster','/data2/wujing03/vu-exec','/etc/video-use-cluster'}
        if NAMESPACE!='dynamic-agent':forbidden.update({
            '/data2/wujing03/distributed-agent-system/.cluster','/data2/wujing03/da-exec','/etc/distributed-agent-system'})
        if str(path) in forbidden:
            raise ValueError('Main deployment paths are forbidden')
    if len({*PORTS.values(),OBS_PORT,PROXY_PORT})!=4:raise ValueError('Dedicated ports must be unique')
    if len(set(UIDS.values()))!=2 or abs(UIDS['a']-UIDS['b'])<64:raise ValueError('Dedicated UID ranges overlap')
    if NAMESPACE!='dynamic-agent':
        source_values={'namespace':'dynamic-agent','database':'distributed_agent_system',
            'role':'distributed_agent_system','orchestrator_role':'distributed_agent_orchestrator',
            'entry':'/distributed-agent-system','object_prefix':'distributed-agent-system',
            'stream_prefix':'distributed-agent-system:events:','media_prefix':'da-media-'}
        current={'namespace':NAMESPACE,'database':DATABASE,'role':ROLE,
            'orchestrator_role':ORCHESTRATOR_ROLE,'entry':ENTRY,'object_prefix':OBJECT_PREFIX,
            'stream_prefix':STREAM_PREFIX,'media_prefix':MEDIA_RUNTIME_PREFIX}
        reused=[name for name,value in current.items() if value==source_values[name]]
        if reused:raise ValueError('Independent deployment reuses source identifiers: '+','.join(reused))
        if set(PORTS.values())&{19766,19767} or {OBS_PORT,PROXY_PORT}&{19765,19768}:
            raise ValueError('Independent deployment reuses source ports')


def configure(seed):
    ROOT.mkdir(mode=0o711,exist_ok=True);ROOT.chmod(0o711)
    CONF.mkdir(mode=0o700,exist_ok=True)
    GPU_LEASE_ROOT.mkdir(mode=0o700,parents=True,exist_ok=True)
    for user in (NAMESPACE,NAMESPACE+'-orch'):
        try:pwd.getpwnam(user)
        except KeyError:run(['useradd','--system','--user-group','--no-create-home','--shell','/usr/sbin/nologin',user])
    run(['groupadd','--system',NAMESPACE+'-runtime'],check=False)
    for name in ('.env','.agent.env','.storage.env'):
        target=REPO/name
        if target.exists():continue
        values=env_file(seed/name)
        if name=='.storage.env':values.update(VIDEO_USE_OBJECT_PREFIX=OBJECT_PREFIX,
            VIDEO_USE_OBJECT_REQUIRE_ENCRYPTION='1',VIDEO_USE_OBJECT_ENCRYPTION_KEY=base64.b64encode(secrets.token_bytes(32)).decode())
        private_env(target,values)
    if not (REPO/'.distributed.env').exists():
        private_env(REPO/'.distributed.env',{
            'VIDEO_USE_DEPLOYMENT':NAMESPACE,'VIDEO_USE_DISTRIBUTED':'1',
            'VIDEO_USE_DATABASE_URL':f'postgresql://{ROLE}:{secrets.token_hex(24)}@127.0.0.1:{PG_PORT}/{DATABASE}',
            'VIDEO_USE_SESSION_SECRET':secrets.token_hex(32),'VIDEO_USE_AGENT_STREAM_PREFIX':STREAM_PREFIX,
            'VIDEO_USE_CANCEL_POLL_SECONDS':'.5','VIDEO_USE_DRAIN_SECONDS':'30',**CAPACITY_ENV,
            'VIDEO_USE_TASK_MEMORY_BYTES':str(12*1024**3),
            'VIDEO_USE_TASK_DISK_BYTES':str(2*1024**3),'VIDEO_USE_WORKSPACE_STORAGE':'disk',
            'VIDEO_USE_GPU_LEASE_ROOT':str(GPU_LEASE_ROOT),
            'VIDEO_USE_EXPECT_ORCHESTRATOR':'1','VIDEO_USE_OBSERVATION_API_PORTS':','.join(map(str,PORTS.values()))})
    private_env(REPO/'.distributed.env',{**env_file(REPO/'.distributed.env'),
        **CAPACITY_ENV,
        'VIDEO_USE_OBSERVATION_API_PORTS':','.join(map(str,PORTS.values())),
        'VIDEO_USE_DYNAMIC_WORKFLOW':'1'})
    if not (CONF/'observation.env').exists():
        private_env(CONF/'observation.env',{'VIDEO_USE_OBSERVATION_TOKEN':secrets.token_hex(32),
            'VIDEO_USE_OBSERVATION_URL':f'http://127.0.0.1:{OBS_PORT}','VIDEO_USE_TELEMETRY_ENABLED':'1',
            'TMPDIR':ROOT/'observability/tmp'})
    service=pwd.getpwnam(NAMESPACE)
    sandbox_values=env_file(CONF/'opensandbox-runtime.env') if (CONF/'opensandbox-runtime.env').exists() else {}
    allowed_sandbox={'VIDEO_USE_CPU_AGENT_SANDBOX_BACKEND','VIDEO_USE_CPU_COMMAND_SANDBOX_BACKEND',
        'VIDEO_USE_OPEN_SANDBOX_URL','VIDEO_USE_OPEN_SANDBOX_API_KEY',
        'VIDEO_USE_OPEN_SANDBOX_NETWORK_ISOLATION','VIDEO_USE_OPEN_SANDBOX_INTERNAL_BRIDGE_CONFIRMED',
        'VIDEO_USE_OPEN_SANDBOX_COMMAND_RUNTIME_PROFILE','VIDEO_USE_OPEN_SANDBOX_COMMAND_IMAGE_DIGEST',
        'VIDEO_USE_OPEN_SANDBOX_COMMAND_BUNDLE_DIGEST'}
    if set(sandbox_values)-allowed_sandbox:raise ValueError('Unknown OpenSandbox runtime deployment setting')
    for node,port in PORTS.items():
        for role in ('api','node','media'):
            path=data(role,node);path.mkdir(mode=0o700,exist_ok=True)
            (path/'tmp').mkdir(mode=0o700,exist_ok=True)
            if role=='api':
                os.chown(path,service.pw_uid,service.pw_gid);os.chown(path/'tmp',service.pw_uid,service.pw_gid)
        registry(node).mkdir(mode=0o700,parents=True,exist_ok=True)
        private_env(CONF/f'api-{node}.env',{'VIDEO_USE_DATA_DIR':data('api',node),'TMPDIR':data('api',node)/'tmp',
            'VIDEO_USE_API_PORT':port,'VIDEO_USE_MEDIA_SOCKET':f'/run/{MEDIA_RUNTIME_PREFIX}{node}/decoder.sock'})
        private_env(CONF/f'node-{node}.env',{'VIDEO_USE_DATA_DIR':data('node',node),'TMPDIR':data('node',node)/'tmp',
            'VIDEO_USE_NODE_INSTANCE':node,
            'VIDEO_USE_EXECUTOR_ROOT':registry(node),'VIDEO_USE_UID_START':UIDS[node],'VIDEO_USE_UID_COUNT':64,
            'VIDEO_USE_GPU_DEVICES':'1' if node=='a' else '2','VIDEO_USE_NODE_OPERATIONS':'*',**sandbox_values})
        private_env(CONF/f'media-{node}.env',{'TMPDIR':data('media',node)/'tmp','VIDEO_USE_DEPLOYMENT':NAMESPACE})
    (ROOT/'observability/tmp').mkdir(mode=0o700,parents=True,exist_ok=True)
    (ROOT/'orchestrator').mkdir(mode=0o700,exist_ok=True)
    (ROOT/'orchestrator/tmp').mkdir(mode=0o700,exist_ok=True)
    orchestrator=pwd.getpwnam(NAMESPACE+'-orch');os.chown(ROOT/'orchestrator',orchestrator.pw_uid,orchestrator.pw_gid);os.chown(ROOT/'orchestrator/tmp',orchestrator.pw_uid,orchestrator.pw_gid)
    values=environment(source=REPO);validate_isolation(values)
    if not (CONF/'orchestrator-database.env').exists():
        private_env(CONF/'orchestrator-database.env',{'VIDEO_USE_DATABASE_URL':
            f'postgresql://{ORCHESTRATOR_ROLE}:{secrets.token_hex(24)}@127.0.0.1:{PG_PORT}/{DATABASE}'})
    private_env(CONF/'orchestrator.env',{key:values[key] for key in (
        'VIDEO_USE_DEPLOYMENT','VIDEO_USE_DISTRIBUTED','VIDEO_USE_DATABASE_URL','VIDEO_USE_OBJECT_BUCKET',
        'VIDEO_USE_AGENT_STREAM_PREFIX','VIDEO_USE_TELEMETRY_ENABLED')}|{
        'VIDEO_USE_DATA_DIR':ROOT/'orchestrator','TMPDIR':ROOT/'orchestrator/tmp','VIDEO_USE_STORAGE_METADATA_ONLY':'1',
        'AWS_EC2_METADATA_DISABLED':'true','PYTHONDONTWRITEBYTECODE':'1'}|env_file(CONF/'orchestrator-database.env'))
    Path('/etc/systemd/journald@'+NAMESPACE+'.conf').write_text('[Journal]\nStorage=persistent\nSystemMaxUse=512M\nRuntimeMaxUse=64M\nSystemKeepFree=1G\nMaxRetentionSec=7day\n')


def prepare_database():
    from urllib.parse import urlsplit
    password=urlsplit(env_file(REPO/'.distributed.env')['VIDEO_USE_DATABASE_URL']).password
    orchestrator_password=urlsplit(env_file(CONF/'orchestrator-database.env')['VIDEO_USE_DATABASE_URL']).password
    prefix=['runuser','-u','postgres','--',PG_BIN/'psql','-X','-v','ON_ERROR_STOP=1','-h',PG_SOCKET,'-p',str(PG_PORT),'-d','postgres']
    sql=f"SELECT 'CREATE ROLE {ROLE} LOGIN PASSWORD ''{password}''' WHERE NOT EXISTS(SELECT 1 FROM pg_roles WHERE rolname='{ROLE}')\n\\gexec\nSELECT 'CREATE DATABASE {DATABASE} OWNER {ROLE}' WHERE NOT EXISTS(SELECT 1 FROM pg_database WHERE datname='{DATABASE}')\n\\gexec\n"
    run(prefix,input=sql)
    run(prefix,input=f"SELECT 'CREATE ROLE {ORCHESTRATOR_ROLE} LOGIN PASSWORD ''{orchestrator_password}''' WHERE NOT EXISTS(SELECT 1 FROM pg_roles WHERE rolname='{ORCHESTRATOR_ROLE}')\n\\gexec\n")


def template_config(source):
    return systemd_templates.ServiceConfig(
        repo=REPO,root=ROOT,release=source,conf=CONF,executors=EXECUTORS,
        namespace=NAMESPACE,service_user=NAMESPACE,database_service='video-use-db.service',
        root_path=ENTRY,agent_runtime=NAMESPACE+'-agent-%i',media_runtime_prefix=MEDIA_RUNTIME_PREFIX,
        gpu_lease_root=GPU_LEASE_ROOT,
        require_database=False,worker_memory_max='28G',worker_cpu_quota='600%',
        worker_tasks_max=1024,observer_port=OBS_PORT)


def install_units(source):
    config=template_config(source)
    units={NAMESPACE+'-api@.service':systemd_templates.api_unit(config),
           NAMESPACE+'-executor@.service':systemd_templates.worker_unit(config),
           NAMESPACE+'-watchdog@.service':systemd_templates.reaper_unit(config),
           NAMESPACE+'-media@.service':systemd_templates.media_unit(config),
           OBSERVER:systemd_templates.observer_unit(config)}
    units[ORCHESTRATOR]=f'''[Unit]
Description=Dynamic workflow deterministic orchestrator
After=network.target video-use-db.service
[Service]
User={NAMESPACE}-orch
Group={NAMESPACE}-orch
WorkingDirectory={source}
EnvironmentFile={CONF}/orchestrator.env
Environment=VIDEO_USE_RELEASE={source.name}
Environment=VIDEO_USE_INSTANCE=%n
ExecStart={source}/.venv/bin/python -m webapp.workflow_orchestrator
Restart=on-failure
RestartSec=3
TimeoutStopSec=30
LogNamespace={NAMESPACE}
ProtectSystem=strict
ProtectHome=true
BindPaths={ROOT}/orchestrator/tmp:/tmp
NoNewPrivileges=true
CapabilityBoundingSet=
ReadWritePaths={ROOT}/orchestrator
InaccessiblePaths={ROOT}/node-a {ROOT}/node-b {EXECUTORS} /data2/wujing03/video-use
MemoryMax=512M
CPUQuota=100%
TasksMax=64
UMask=0077
[Install]
WantedBy=multi-user.target
'''
    for name,text in units.items():(Path('/etc/systemd/system')/name).write_text(text)
    for node in PORTS:
        other='b' if node=='a' else 'a'
        for role in ('api','executor','watchdog'):
            folder=Path('/etc/systemd/system')/f'{NAMESPACE}-{role}@{node}.service.d';folder.mkdir(exist_ok=True)
            hidden=[Path('/data2/wujing03/video-use'),Path('/data2/wujing03/vu-exec'),
                    data('node',other),registry(other)]
            if NAMESPACE!='dynamic-agent':hidden += [Path('/data2/wujing03/distributed-agent-system'),Path('/data2/wujing03/da-exec')]
            if role=='api':hidden += [data('node',node),registry(node),data('api',other)]
            (folder/'isolation.conf').write_text('[Service]\nInaccessiblePaths='+' '.join(map(str,hidden))+'\n')
    run(['systemctl','daemon-reload'])


def prepare_runtime(source):
    env={**os.environ,'VIDEO_USE_DEPLOYMENT':NAMESPACE,'VIDEO_USE_EXECUTOR_ROOT':str(EXECUTORS)}
    for account in (NAMESPACE,NAMESPACE+'-orch'):
        for parent in [source,*source.parents,*(source/'.venv').resolve().parents]:
            if parent==Path('/'):break
            run(['setfacl','-m','u:'+account+':--x',parent])
    run(['bash',source/'deploy/prepare-executor.sh','--models'],env=env,cwd=source,timeout=300)
    run([source/'.venv/bin/python','-c',f'from pathlib import Path; from deploy.runtime_access import grant; grant(Path(".venv"),users=({NAMESPACE+"-orch"!r},))'],env=env,cwd=source,timeout=180)
    for directory in (source/'webapp',source/'helpers',source/'references',source/'.venv'):
        run(['setfacl','-R','-m','u:'+NAMESPACE+'-orch:rX',directory.resolve()],timeout=180)
    run(['setfacl','-m','u:'+NAMESPACE+'-orch:rx',source])


def freeze():
    from tests.run_workflow_suite import source_digest
    original=source_digest();target=ROOT/'releases'/original[:12]
    if not target.exists():
        target.mkdir(parents=True,mode=0o755)
        for name in ('webapp','helpers','references','runtime/public','runtime/opensandbox','tests','deploy','docs'):
            shutil.copytree(REPO/name,target/name,ignore=shutil.ignore_patterns('__pycache__','*.pyc'))
        for name in ('SKILL.md','AGENTS.md','pyproject.toml','uv.lock','uv.toml','deploy.sh','.gitignore','README.md','WEB_UI.md','UI_DESIGN.md','video-use-dynamic-agent-workflow-design.md'):
            shutil.copyfile(REPO/name,target/name)
        dependency=ROOT/'venvs'/hashlib.sha256((REPO/'uv.lock').read_bytes()).hexdigest()[:16]
        if not dependency.exists():
            dependency.parent.mkdir(parents=True,exist_ok=True)
            run(['cp','-a','--reflink=auto',REPO/'.venv',dependency],timeout=180)
        (target/'.venv').symlink_to(dependency,target_is_directory=True)
    if common.digest(target,runner=run)!=original:raise ValueError('Frozen source differs from working tree')
    return target,original


def verify(source,selection=()):
    """A readable source-only copy keeps test service identities out of deployment data."""
    import tempfile
    directory=Path(tempfile.mkdtemp(prefix='das-verify-',dir=REPO.parent/'vu-tmp'));directory.chmod(0o755)
    for name in ('webapp','helpers','references','runtime/public','runtime/opensandbox','tests','deploy','docs'):
        shutil.copytree(source/name,directory/name,ignore=shutil.ignore_patterns('__pycache__','*.pyc'))
    for name in ('SKILL.md','AGENTS.md','pyproject.toml','uv.lock','uv.toml','deploy.sh','.gitignore','README.md','WEB_UI.md','UI_DESIGN.md','video-use-dynamic-agent-workflow-design.md'):
        shutil.copyfile(source/name,directory/name)
    for path in directory.rglob('*'):
        if not path.is_symlink():path.chmod(0o755 if path.is_dir() or path.stat().st_mode&0o111 else 0o644)
    # Do not traverse the private deployment root as the default test service
    # identity: an unreadable pyvenv.cfg silently falls back to system Python.
    run(['cp','-a','--reflink=auto',REPO/'.venv',directory/'.venv'],timeout=180)
    run(['setfacl','-R','-m','u:video-use:rX,g:video-use-runtime:rX',directory/'.venv'],timeout=180)
    (directory/'runtime/build').mkdir(parents=True)
    shutil.copyfile(source/'runtime/build/manifest.json',directory/'runtime/build/manifest.json')
    log=ROOT/'verification'/f'{source.name}.log';log.parent.mkdir(parents=True,exist_ok=True)
    with log.open('w') as stream:
        test_tmp=ROOT/'tmp/verification';test_tmp.mkdir(parents=True,exist_ok=True,mode=0o711);test_tmp.chmod(0o711)
        socket_root=test_tmp/('s-'+source.name);socket_root.mkdir(parents=True,exist_ok=True)
        test_environment={**os.environ,'TMPDIR':str(test_tmp),'TMP':str(test_tmp),'TEMP':str(test_tmp),
                          'VIDEO_USE_TEST_TMP_ROOT':str(test_tmp),'VIDEO_USE_SOCKET_ROOT':str(socket_root)}
        result=subprocess.run([directory/'.venv/bin/python','-m','tests.run_distributed_suite',*selection],cwd=directory,
            env=test_environment,stdout=stream,stderr=subprocess.STDOUT,timeout=1800)
        if result.returncode:raise RuntimeError('Python verification failed: '+str(log))
        if not selection:
            result=subprocess.run(['node','--test',*map(str,sorted((directory/'tests').glob('*.test.mjs')))],cwd=directory,
                env=test_environment,stdout=stream,stderr=subprocess.STDOUT,timeout=300)
            if result.returncode:raise RuntimeError('Frontend verification failed: '+str(log))
    report=directory/'docs/test-evidence/distributed-suite.json'
    if json.loads(report.read_text())['source_sha256']!=common.digest(source,runner=run):raise ValueError('Verification source mismatch')
    (source/'docs/test-evidence').mkdir(exist_ok=True,parents=True)
    shutil.copyfile(report,source/'docs/test-evidence/distributed-suite.json')
    return log


def gate(source):
    report=json.loads((source/'docs/test-evidence/distributed-suite.json').read_text())
    if not report['passed'] or report['selection']!=['all Python tests'] or report['source_sha256']!=common.digest(source,runner=run):
        raise ValueError('Full verification for this exact release is required')
    validate_isolation(environment(source=source))


def initialize(source):
    # Initialize lazy object-metadata tables with the business role before the
    # metadata-only orchestrator receives its table grants. Fresh databases do
    # not have historical API traffic to create these tables implicitly.
    code='from pathlib import Path; from webapp.store import Store; from webapp import agent_store,image_api; from webapp.paths import data_root; s=Store(data_root()); s.artifacts; agent_store.initialize(s); image_api.initialize(s)'
    run([source/'.venv/bin/python','-c',code],cwd=source,env=environment(source=source),timeout=120)
    # The control plane may read receipts but never provider cookies or
    # authorization material. No model/BOSS credentials enter its environment.
    sql=f'''BEGIN;
    GRANT CONNECT ON DATABASE {DATABASE} TO {ORCHESTRATOR_ROLE};
    GRANT USAGE ON SCHEMA public TO {ORCHESTRATOR_ROLE};
    GRANT SELECT,INSERT,UPDATE,DELETE ON ALL TABLES IN SCHEMA public TO {ORCHESTRATOR_ROLE};
    GRANT USAGE,SELECT ON ALL SEQUENCES IN SCHEMA public TO {ORCHESTRATOR_ROLE};
    REVOKE ALL ON workflow_provider_calls FROM {ORCHESTRATOR_ROLE};
    GRANT SELECT(request_key,workflow,step,revision,provider,operation,state,external_id,result,implementation,created,updated)
        ON workflow_provider_calls TO {ORCHESTRATOR_ROLE};
    COMMIT;'''
    run(['runuser','-u','postgres','--',PG_BIN/'psql','-X','-v','ON_ERROR_STOP=1','-h',PG_SOCKET,'-p',str(PG_PORT),'-d',DATABASE],input=sql)
    source_demos=REPO/'workspace/demos'
    if (source_demos/'catalog.json').exists():
        run([source/'.venv/bin/python','-m','webapp.demo_library','--source',source_demos],cwd=source,env=environment(source=source),timeout=900)


def nginx_config(maintenance=False):
    # A bounded API queue returning 503 is alive. Treating that response as
    # peer failure ejects both replicas during a burst and turns overload into 502.
    return systemd_templates.nginx_config(namespace=NAMESPACE,proxy_port=PROXY_PORT,
        api_ports=PORTS.values(),maintenance=maintenance,retry_503=False)


def proxy(maintenance=False):
    base='distributed-agent-system' if NAMESPACE=='dynamic-agent' else NAMESPACE
    target=Path('/etc/nginx/conf.d')/('00-'+base+'.conf')
    text=nginx_config(maintenance)
    snippet=Path('/etc/nginx/snippets')/(base+'.conf')
    fragment=f'''location = {ENTRY} {{ return 301 {ENTRY}/; }}
location ^~ {ENTRY}/ {{
    proxy_pass http://127.0.0.1:{PROXY_PORT}/;
    proxy_http_version 1.1;
    proxy_set_header Host $host;
    proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
    proxy_set_header X-Forwarded-Proto $scheme;
    proxy_set_header Connection "";
    proxy_read_timeout 600s;
    proxy_send_timeout 600s;
    client_max_body_size 2048m;
    proxy_request_buffering off;
    proxy_buffering off;
}}
'''
    server=Path('/etc/nginx/conf.d/updream-lab.conf');original=server.read_text()
    include='    include '+str(snippet)+';'
    updated=original if include in original else original.replace('    include /etc/nginx/snippets/video-use.conf;', '    include /etc/nginx/snippets/video-use.conf;\n'+include)
    if include not in updated:raise ValueError('Public server include anchor was not found')
    prior={p:p.read_text() if p.exists() else None for p in (target,snippet,server)}
    try:
        target.write_text(text);snippet.write_text(fragment);server.write_text(updated)
        run(['nginx','-t']);run(['systemctl','reload','nginx'])
    except BaseException:
        for p,value in prior.items():
            if value is None:p.unlink(missing_ok=True)
            else:p.write_text(value)
        raise


def stop():
    if (ROOT/'observability').exists():(ROOT/'observability/maintenance').touch(mode=0o600)
    base='distributed-agent-system' if NAMESPACE=='dynamic-agent' else NAMESPACE
    if (Path('/etc/nginx/conf.d')/('00-'+base+'.conf')).exists():proxy(True)
    output=run(['systemctl','list-units','--all','--plain','--no-legend',NAMESPACE+'-qa-orchestrator-*.service'],check=False).stdout
    import re
    extra=[line.split()[0] for line in output.splitlines() if line.split() and re.fullmatch(NAMESPACE+r'-qa-orchestrator-[a-f0-9]{12}\.service',line.split()[0])]
    parallel_service('stop',[s for s in APIS+WORKERS+[ORCHESTRATOR]+extra if active(s)])
    for _ in range(60):
        if not any(list((registry(n)/'records').glob('*.json')) for n in PORTS):break
        time.sleep(1)
    else:raise RuntimeError('Cleanup remains pending; independent watchdogs retained')
    parallel_service('stop',[s for s in REAPERS+MEDIA if active(s)])


def health():
    import requests
    client=requests.Session();client.trust_env=False
    for _ in range(45):
        try:
            responses=[client.get(f'http://127.0.0.1:{p}/api/agent/health',timeout=2) for p in PORTS.values()]
            if all(response.ok and response.json().get('available') for response in responses) and all(active(name) for name in SERVICES):return
        except requests.RequestException:pass
        time.sleep(1)
    raise RuntimeError('Dynamic services did not become healthy')


def start():
    parallel_service('start',REAPERS+MEDIA)
    parallel_service('start',APIS+WORKERS+[ORCHESTRATOR,OBSERVER])
    health();proxy();(ROOT/'observability/maintenance').unlink(missing_ok=True)


def status():
    print(json.dumps({'services':{s:active(s) for s in SERVICES},'entrypoint':ENTRY+'/',
        'database':DATABASE,'object_prefix':OBJECT_PREFIX,'stream_prefix':STREAM_PREFIX,'ports':PORTS,
        'release':json.loads((ROOT/'release.json').read_text()) if (ROOT/'release.json').exists() else None},indent=2))


def reset_database():
    """Explicit fresh-schema deployment; never migrate or touch the shared server."""
    if not DATABASE or not ROLE or DATABASE in ('main','postgres','video_use'):
        raise ValueError('Database reset is restricted to a dedicated deployment')
    validate_isolation(environment())
    if any(active(service) for service in SERVICES):raise ValueError('Stop all dedicated services before database reset')
    if any(list((registry(node)/'records').glob('*.json')) for node in PORTS):
        raise ValueError('Executor cleanup must finish before database reset')
    sql=f'''BEGIN;
    DO $reset$
    DECLARE unresolved bigint;
    BEGIN
      IF current_database() <> '{DATABASE}' THEN RAISE EXCEPTION 'Wrong reset target'; END IF;
      IF to_regclass('public.workflow_provider_calls') IS NOT NULL THEN
        EXECUTE $query$SELECT COUNT(*) FROM workflow_provider_calls WHERE state NOT IN ('succeeded','failed','cancelled','rejected')$query$ INTO unresolved;
        IF unresolved > 0 THEN RAISE EXCEPTION 'Unresolved Provider calls must be reconciled before reset'; END IF;
      END IF;
      IF to_regclass('public.execution_cleanup') IS NOT NULL THEN
        EXECUTE $query$SELECT COUNT(*) FROM execution_cleanup WHERE state <> 'complete'$query$ INTO unresolved;
        IF unresolved > 0 THEN RAISE EXCEPTION 'Pending execution cleanup prevents reset'; END IF;
      END IF;
      IF to_regclass('public.execution_external_cleanup') IS NOT NULL THEN
        EXECUTE $query$SELECT COUNT(*) FROM execution_external_cleanup WHERE state <> 'complete'$query$ INTO unresolved;
        IF unresolved > 0 THEN RAISE EXCEPTION 'Unresolved external cleanup prevents reset'; END IF;
      END IF;
    END $reset$;
    DROP SCHEMA public CASCADE;
    CREATE SCHEMA public AUTHORIZATION {ROLE};
    GRANT USAGE ON SCHEMA public TO {ORCHESTRATOR_ROLE};
    COMMIT;'''
    run(['runuser','-u','postgres','--',PG_BIN/'psql','-X','-v','ON_ERROR_STOP=1',
         '-h',PG_SOCKET,'-p',str(PG_PORT),'-d',DATABASE],input=sql)


def deploy(seed,*,reset=False,targeted_unverified=False):
    configure(seed);prepare_database()
    source,sha=freeze();prepare_runtime(source)
    if targeted_unverified:validate_isolation(environment(source=source))
    else:
        try:gate(source)
        except (FileNotFoundError,ValueError):verify(source);gate(source)
    if any(active(s) for s in APIS+WORKERS+[ORCHESTRATOR]):stop()
    if active(OBSERVER):run(['systemctl','stop',OBSERVER])
    backup=ROOT/'backups'/time.strftime('%Y%m%dT%H%M%S');backup.mkdir(parents=True,mode=0o700)
    if (ROOT/'release.json').exists():shutil.copyfile(ROOT/'release.json',backup/'release.json')
    with (backup/'database.dump').open('wb') as output:
        (backup/'database.dump').chmod(0o600)
        result=subprocess.run(['runuser','-u','postgres','--',PG_BIN/'pg_dump','-h',PG_SOCKET,'-p',str(PG_PORT),'-Fc',DATABASE],stdout=output,stderr=subprocess.PIPE,timeout=120)
        if result.returncode:raise RuntimeError('Dedicated database backup failed')
    if reset:reset_database()
    initialize(source);install_units(source)
    run(['systemctl','enable',*SERVICES])
    if active(OBSERVER):run(['systemctl','restart',OBSERVER])
    start()
    private_env(ROOT/'runtime.env',{'VIDEO_USE_DEPLOYMENT':NAMESPACE})
    (ROOT/'release.json').write_text(json.dumps({'source':str(source),'sha256':sha,'deployed_at':time.time(),'namespace':NAMESPACE,
        'verification':'targeted_only' if targeted_unverified else 'full_gate'},indent=2)+'\n')
    print('Independent dynamic workflow deployment ready: '+ENTRY+'/')


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action',choices=['deploy','configure','start','stop','restart','status','check'],nargs='?',default='status')
    parser.add_argument('--seed-from',type=Path,default=Path('/data2/wujing03/video-use'))
    parser.add_argument('--reset-database',action='store_true',help='After stopping dedicated services and backing up, replace only the dedicated schema; unresolved calls block reset')
    parser.add_argument('--targeted-unverified',action='store_true',help='Isolated targeted retest only: deploy without a full gate and mark the release ineligible for a full campaign')
    args=parser.parse_args();os.chdir(REPO)
    if (args.reset_database or args.targeted_unverified) and args.action!='deploy':parser.error('--reset-database and --targeted-unverified require deploy')
    if os.geteuid()!=0:raise SystemExit('Deployment requires root')
    with CONTROL_LOCK.open('a') as lock:
        fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
        if args.action=='deploy':deploy(args.seed_from,reset=args.reset_database,targeted_unverified=args.targeted_unverified)
        elif args.action=='configure':configure(args.seed_from);prepare_database()
        elif args.action=='restart':stop();start()
        elif args.action=='check':gate(release());health()
        else:globals()[args.action]()


if __name__=='__main__':main()
