"""Single-host, independently rooted distributed installation. Never deletes data."""
import argparse
import concurrent.futures
import fcntl
import hashlib
import json
import os
from pathlib import Path
import pwd
import secrets
import shlex
import shutil
import sqlite3
import subprocess
import sys
import time

REPO=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(REPO))
RELEASE=REPO
ROOT=REPO/'.cluster'
EXECUTORS=REPO.parent/'vu-exec'
CONF=Path('/etc/video-use-cluster')
PG_BIN=Path('/usr/lib/postgresql/16/bin')
DB_PORT=15432
PORTS={'a':18766,'b':18767}
UIDS={'a':700000,'b':700100}
GPUS={'a':'0','b':'3'}
DB_SERVICE='video-use-db.service'
OBS_SERVICE='video-use-observer.service'
APIS=[f'video-use-api@{n}.service' for n in PORTS]
WORKERS=[f'video-use-executor@{n}.service' for n in PORTS]
REAPERS=[f'video-use-watchdog@{n}.service' for n in PORTS]
MEDIA=[f'video-use-media@{n}.service' for n in PORTS]
LEGACY=['video-use.service','video-use-agent.service','video-use-reaper.service']
PROXY=Path('/etc/nginx/conf.d/00-video-use-cluster.conf')


def run(args,*,env=None,input=None,check=True,timeout=120,cwd=None):
    result=subprocess.run([str(x) for x in args],env=env,input=input,text=True,capture_output=True,timeout=timeout,cwd=cwd)
    if check and result.returncode:
        # Private command input/environment can contain credentials. Never print it.
        raise RuntimeError(f'{Path(str(args[0])).name} failed (exit {result.returncode}); inspect service logs')
    return result


def env_file(path):
    values={}
    for line in Path(path).read_text().splitlines():
        if not line.strip() or line.lstrip().startswith('#'):continue
        key,value=line.split('=',1);parts=shlex.split(value)
        if len(parts)>1:raise ValueError('Invalid environment entry: '+key)
        values[key]=parts[0] if parts else ''
    return values


def private_env(path,values):
    path=Path(path);path.parent.mkdir(parents=True,exist_ok=True)
    temporary=path.with_suffix(path.suffix+'.new')
    fd=os.open(temporary,os.O_WRONLY|os.O_CREAT|os.O_TRUNC,0o600)
    with os.fdopen(fd,'w') as stream:
        for key,value in sorted(values.items()):stream.write(key+'='+shlex.quote(str(value))+'\n')
    temporary.chmod(0o600);temporary.replace(path)


def environment(node='a'):
    result=dict(os.environ)
    for name in ('.env','.agent.env','.storage.env','.distributed.env'):
        result.update(env_file(REPO/name))
    result.update(env_file(CONF/f'node-{node}.env'))
    return result


def registry(node):return EXECUTORS/node
def data(role,node):return ROOT/(role+'-'+node)


def node_gpu_devices(settings,node):
    raw=settings.get(f'VIDEO_USE_NODE_{node.upper()}_GPU_DEVICES',GPUS[node])
    try:devices=sorted({int(value.strip()) for value in raw.split(',') if value.strip()})
    except (ValueError,AttributeError):raise ValueError('Invalid GPU candidates for node '+node) from None
    if any(value<0 or value>255 for value in devices):raise ValueError('Invalid GPU candidates for node '+node)
    return ','.join(map(str,devices))


def unit_header(description):
    return f'''[Unit]
Description={description}
After=network.target video-use-db.service
Requires=video-use-db.service

[Service]
WorkingDirectory={RELEASE}
EnvironmentFile={REPO}/.env
EnvironmentFile={REPO}/.agent.env
EnvironmentFile={REPO}/.storage.env
EnvironmentFile={REPO}/.distributed.env
Environment=PYTHONDONTWRITEBYTECODE=1
EnvironmentFile={CONF}/observation.env
Environment=VIDEO_USE_RELEASE={RELEASE.name}
Environment=VIDEO_USE_INSTANCE=%n
LogNamespace=video-use
Environment=PYTHONUNBUFFERED=1
Restart=on-failure
RestartSec=5
KillMode=control-group
ProtectHome=true
ProtectSystem=strict
ProtectKernelTunables=true
ProtectKernelModules=true
PrivateTmp=true
LimitNOFILE=4096
'''


def api_unit():
    header=unit_header('Video Use distributed API %i').replace('Requires=video-use-db.service','Requires=video-use-db.service video-use-media@%i.service').replace('After=network.target video-use-db.service','After=network.target video-use-db.service video-use-media@%i.service')
    return header+f'''User=video-use
Group=video-use
EnvironmentFile={CONF}/api-%i.env
ExecStart={RELEASE}/.venv/bin/python -m uvicorn webapp.app:app --host 127.0.0.1 --port ${{VIDEO_USE_API_PORT}} --root-path /video-use --proxy-headers --forwarded-allow-ips 127.0.0.1
ReadWritePaths={ROOT}/api-%i
NoNewPrivileges=true
CapabilityBoundingSet=
RestrictNamespaces=user mnt pid net ipc uts cgroup
TasksMax=256
MemoryMax=2G
CPUQuota=200%
TimeoutStopSec=30
UMask=0077

[Install]
WantedBy=multi-user.target
'''


def worker_unit():
    header=unit_header('Video Use distributed executor %i').replace('Requires=video-use-db.service','Requires=video-use-db.service video-use-watchdog@%i.service').replace('After=network.target video-use-db.service','After=network.target video-use-db.service video-use-watchdog@%i.service')
    return header+f'''User=root
Group=video-use
EnvironmentFile={CONF}/node-%i.env
ExecStart={RELEASE}/.venv/bin/python -m webapp.agent_host
RuntimeDirectory=video-use-agent
RuntimeDirectoryMode=0700
RuntimeDirectoryPreserve=yes
ReadWritePaths={ROOT}/node-%i {registry('%i')} /run/video-use-agent /sys/fs/cgroup /data/wujing03/.media-gpu-leases
NoNewPrivileges=false
ProtectControlGroups=false
RestrictSUIDSGID=true
RestrictNamespaces=mnt pid net ipc uts cgroup
CapabilityBoundingSet=CAP_SYS_ADMIN CAP_SYS_CHROOT CAP_SETUID CAP_SETGID CAP_SETPCAP CAP_CHOWN CAP_FOWNER CAP_DAC_OVERRIDE CAP_KILL CAP_NET_ADMIN
TasksMax=512
MemoryMax=14G
CPUQuota=300%
TimeoutStopSec=90
UMask=0007

[Install]
WantedBy=multi-user.target
'''


def reaper_unit():
    return unit_header('Video Use node watchdog %i')+f'''User=root
EnvironmentFile={CONF}/node-%i.env
ExecStart={RELEASE}/.venv/bin/python -m webapp.execution_reaper
RuntimeDirectory=video-use-agent
RuntimeDirectoryMode=0700
RuntimeDirectoryPreserve=yes
ReadWritePaths={ROOT}/node-%i {registry('%i')} /run/video-use-agent /sys/fs/cgroup
CapabilityBoundingSet=CAP_SYS_ADMIN CAP_SYS_CHROOT CAP_SYS_PTRACE CAP_KILL CAP_DAC_OVERRIDE CAP_FOWNER CAP_CHOWN CAP_SETUID CAP_SETGID CAP_SETPCAP
TasksMax=256
MemoryMax=768M
CPUQuota=100%
TimeoutStopSec=30
UMask=0077

[Install]
WantedBy=multi-user.target
'''


def postgres_unit():
    return f'''[Unit]
Description=Video Use dedicated shared PostgreSQL
After=network.target

[Service]
Type=simple
User=postgres
Group=postgres
RuntimeDirectory=video-use-db
RuntimeDirectoryMode=0700
ExecStart={PG_BIN}/postgres -D {ROOT}/postgres
ExecStop={PG_BIN}/pg_ctl -D {ROOT}/postgres -m fast -w stop
Restart=on-failure
RestartSec=3
TimeoutStopSec=60
ProtectSystem=strict
ProtectHome=true
PrivateTmp=true
ReadWritePaths={ROOT}/postgres /run/video-use-db
MemoryMax=2G
TasksMax=256
UMask=0077

[Install]
WantedBy=multi-user.target
'''


def media_unit():
    return f'''[Unit]
Description=Video Use fixed media decoder %i
After=local-fs.target

[Service]
User=root
Group=video-use
WorkingDirectory={RELEASE}
EnvironmentFile={CONF}/api-%i.env
Environment=VIDEO_USE_TELEMETRY_ENABLED=1
Environment=VIDEO_USE_RELEASE={RELEASE.name}
Environment=VIDEO_USE_INSTANCE=%n
LogNamespace=video-use
Environment=PYTHONDONTWRITEBYTECODE=1
EnvironmentFile={CONF}/media-%i.env
RuntimeDirectory=vu-media-%i
RuntimeDirectoryMode=0750
ExecStart={RELEASE}/.venv/bin/python -m webapp.media_service
Restart=on-failure
RestartSec=2
KillMode=control-group
TimeoutStopSec=50
ProtectSystem=strict
ProtectHome=true
PrivateTmp=true
ReadWritePaths=/run/vu-media-%i {ROOT}/media-%i
ProtectKernelTunables=true
ProtectKernelModules=true
CapabilityBoundingSet=CAP_SYS_ADMIN CAP_SYS_CHROOT CAP_SETUID CAP_SETGID CAP_SETPCAP CAP_CHOWN CAP_FOWNER CAP_DAC_OVERRIDE CAP_NET_ADMIN
MemoryMax=3G
CPUQuota=200%
TasksMax=256
UMask=0077

[Install]
WantedBy=multi-user.target
'''


def nginx_config(maintenance=False):
    if maintenance:return 'server { listen 127.0.0.1:18765; location / { default_type application/json; return 503 \'{"error":"Video Use is stopped for maintenance"}\'; } }\n'
    return '''upstream video_use_cluster {
    least_conn;
    server 127.0.0.1:18766 max_fails=1 fail_timeout=5s;
    server 127.0.0.1:18767 max_fails=1 fail_timeout=5s;
    keepalive 16;
}
server {
    listen 127.0.0.1:18765;
    location / {
        proxy_pass http://video_use_cluster;
        proxy_http_version 1.1;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
        proxy_set_header Connection "";
        proxy_next_upstream error timeout http_502 http_503 http_504;
        proxy_connect_timeout 3s;
        proxy_read_timeout 600s;
        proxy_send_timeout 600s;
        client_max_body_size 2048m;
        proxy_request_buffering off;
        proxy_buffering off;
        add_header X-Video-Use-API $upstream_addr always;
        add_header X-Accel-Buffering no always;
    }
}
'''


def active(name):return run(['systemctl','is-active','--quiet',name],check=False).returncode==0


def parallel_service(action,names):
    with concurrent.futures.ThreadPoolExecutor(max_workers=len(names) or 1) as pool:
        futures=[pool.submit(run,['systemctl',action,name],timeout=150) for name in names]
        for future in futures:future.result()


def proxy(maintenance=False):
    previous=PROXY.read_text() if PROXY.exists() else None
    PROXY.write_text(nginx_config(maintenance));PROXY.chmod(0o644)
    if run(['nginx','-t'],check=False).returncode:
        if previous is None:PROXY.unlink()
        else:PROXY.write_text(previous)
        raise RuntimeError('Nginx configuration rejected; previous configuration restored')
    run(['systemctl','reload','nginx'])


def digest(root):
    result=run([sys.executable,'-c','from tests.run_workflow_suite import source_digest; print(source_digest())'],env={**os.environ,'PYTHONPATH':str(root)},timeout=30) if root==Path.cwd() else subprocess.run([sys.executable,'-c','from tests.run_workflow_suite import source_digest; print(source_digest())'],cwd=root,env={**os.environ,'PYTHONPATH':str(root)},capture_output=True,text=True,check=True)
    return result.stdout.strip()


def gate(source=None):
    source=source or RELEASE
    report=json.loads((source/'docs/test-evidence/distributed-suite.json').read_text())
    if not report.get('passed') or report.get('selection')!=['all Python tests'] or report.get('source_sha256')!=digest(source):
        raise RuntimeError('Run the complete distributed test suite on the current source before deploying')
    for path in (REPO/'.storage.env',REPO/'.agent.env'):
        if not path.is_file() or path.stat().st_mode&0o077:raise RuntimeError('Private service configuration missing or too permissive: '+path.name)
    values=env_file(REPO/'.storage.env')
    if values.get('VIDEO_USE_OBJECT_REQUIRE_ENCRYPTION')!='1' or not values.get('VIDEO_USE_OBJECT_ENCRYPTION_KEY'):
        raise RuntimeError('BOSS deployment requires private object encryption')


def stage_release():
    """Freeze mutable workspace code; test and run this exact immutable snapshot."""
    global RELEASE
    ROOT.mkdir(mode=0o700,exist_ok=True)
    run(['setfacl','-m','u:video-use:--x',ROOT])
    environments=ROOT/'venvs';environments.mkdir(mode=0o755,exist_ok=True)
    run(['setfacl','-m','u:video-use:--x',environments])
    dependency_id=hashlib.sha256((REPO/'uv.lock').read_bytes()).hexdigest()[:16]
    dependency=environments/dependency_id
    if not dependency.exists():
        temporary=environments/(dependency_id+'.new')
        if temporary.exists():shutil.rmtree(temporary)
        run(['cp','-a','--reflink=auto',REPO/'.venv',temporary],timeout=180)
        temporary.rename(dependency)
    base=ROOT/'releases';base.mkdir(mode=0o755,parents=True,exist_ok=True)
    for path in (base.parent,base):run(['setfacl','-m','u:video-use:--x',path])
    before=digest(REPO);target=base/before[:12]
    if not target.exists():
        staging=base/('stage-'+secrets.token_hex(4));staging.mkdir(mode=0o755)
        try:
            for name in ('webapp','helpers','references','runtime/public','tests','deploy','docs'):
                shutil.copytree(REPO/name,staging/name,ignore=shutil.ignore_patterns('__pycache__','*.pyc'))
            for name in ('SKILL.md','pyproject.toml','uv.lock','uv.toml','deploy.sh','.gitignore'):
                shutil.copyfile(REPO/name,staging/name)
            (staging/'.venv').symlink_to(dependency,target_is_directory=True)
            if digest(REPO)!=before or digest(staging)!=before:raise RuntimeError('Workspace changed while freezing release; retry deploy')
            staging.rename(target)
        except BaseException:
            shutil.rmtree(staging,ignore_errors=True);raise
    if digest(target)!=before:raise RuntimeError('Existing release snapshot was modified')
    RELEASE=target
    print('Frozen release:',target,flush=True)
    result=run(['bash',RELEASE/'deploy/prepare-executor.sh','--models'],timeout=300)
    print(result.stdout,flush=True)
    try:gate(RELEASE)
    except (FileNotFoundError,RuntimeError):
        log=RELEASE/'verification.log'
        print('Running complete verification; log:',log,flush=True)
        # UNIX sockets in privileged fixtures need a short absolute parent.
        test_tmp=REPO.parent/'vu-tmp';test_tmp.mkdir(mode=0o711,exist_ok=True);test_tmp.chmod(0o711)
        with log.open('w') as output:
            result=subprocess.run([RELEASE/'.venv/bin/python','-m','tests.run_distributed_suite'],cwd=RELEASE,
                env={**os.environ,'TMPDIR':str(test_tmp),'VIDEO_USE_TEST_REAL_MODELS':'1'},stdout=output,stderr=subprocess.STDOUT,timeout=1200)
        if result.returncode:raise RuntimeError('Frozen release verification failed; inspect '+str(log))
        gate(RELEASE)
    shutil.copyfile(RELEASE/'docs/test-evidence/distributed-suite.json',REPO/'docs/test-evidence/cluster-release-tests.json')


def backup():
    folder=ROOT/'backups'/('cluster-'+time.strftime('%Y%m%dT%H%M%SZ',time.gmtime()))
    folder.mkdir(parents=True,mode=0o700)
    run(['tar','--exclude=__pycache__','--exclude=*.pyc','-czf',folder/'source.tar.gz','-C',RELEASE,'webapp','helpers','runtime/public','references','deploy','deploy.sh','pyproject.toml','uv.lock'])
    for name in ('.env','.agent.env','.storage.env','.distributed.env'):
        source=REPO/name
        if source.exists():shutil.copyfile(source,folder/name);(folder/name).chmod(0o600)
    for source in CONF.glob('*.env') if CONF.exists() else []:
        shutil.copyfile(source,folder/('instance-'+source.name));(folder/('instance-'+source.name)).chmod(0o600)
    if (REPO/'workspace/state.sqlite3').exists():
        with sqlite3.connect('file:'+str(REPO/'workspace/state.sqlite3')+'?mode=ro',uri=True) as src,sqlite3.connect(folder/'legacy.sqlite3') as dst:src.backup(dst)
    if active(DB_SERVICE):
        with (folder/'metadata.dump').open('wb') as output:
            result=subprocess.run(['runuser','-u','postgres','--',str(PG_BIN/'pg_dump'),'-h','/run/video-use-db','-p',str(DB_PORT),'-Fc','video_use'],stdout=output,stderr=subprocess.PIPE,timeout=120)
        if result.returncode:raise RuntimeError('Cannot back up cluster database')
    (folder/'services.txt').write_text('\n'.join(run(['systemctl','cat',name],check=False).stdout for name in LEGACY))
    if PROXY.exists():shutil.copyfile(PROXY,folder/'nginx.conf')
    for path in folder.iterdir():path.chmod(0o600)
    print('Backup:',folder,flush=True)
    return folder


def observer_unit():
    return f'''[Unit]
Description=Video Use observations and diagnostics
After=network.target systemd-journald@video-use.service

[Service]
WorkingDirectory={RELEASE}
EnvironmentFile={REPO}/.env
EnvironmentFile={REPO}/.agent.env
EnvironmentFile={REPO}/.storage.env
EnvironmentFile={REPO}/.distributed.env
EnvironmentFile={CONF}/node-a.env
EnvironmentFile={CONF}/observation.env
Environment=VIDEO_USE_SERVICE=observer
Environment=VIDEO_USE_RELEASE={RELEASE.name}
ExecStart={RELEASE}/.venv/bin/python -m webapp.observation_collector --root {ROOT}/observability
Restart=on-failure
RestartSec=3
ProtectSystem=strict
ProtectHome=true
PrivateTmp=true
ReadWritePaths={ROOT}/observability
NoNewPrivileges=true
MemoryMax=512M
CPUQuota=100%
TasksMax=64
UMask=0077
LogNamespace=video-use

[Install]
WantedBy=multi-user.target
'''


def configure():
    ROOT.mkdir(mode=0o700,exist_ok=True);CONF.mkdir(mode=0o700,exist_ok=True)
    # Traversal only: children and private configurations remain independently
    # protected. Workers need not mutate a read-only parent to grant broker access.
    ROOT.chmod(0o711)
    observation=CONF/'observation.env'
    if not observation.exists():
        private_env(observation,{'VIDEO_USE_OBSERVATION_TOKEN':secrets.token_hex(32),'VIDEO_USE_OBSERVATION_URL':'http://127.0.0.1:18768',
            'VIDEO_USE_TELEMETRY_ENABLED':'1','VIDEO_USE_OBSERVATION_MAX_ROWS':'200000','VIDEO_USE_OBSERVATION_RETENTION_SECONDS':'604800'})
    (ROOT/'observability').mkdir(mode=0o700,exist_ok=True)
    (ROOT/'observability/tmp').mkdir(mode=0o700,exist_ok=True)
    private_env(observation,{**env_file(observation),'TMPDIR':ROOT/'observability/tmp'})
    Path('/etc/systemd/journald@video-use.conf').write_text('[Journal]\nStorage=persistent\nSystemMaxUse=512M\nRuntimeMaxUse=64M\nSystemKeepFree=1G\nMaxRetentionSec=7day\nRateLimitIntervalSec=30s\nRateLimitBurst=10000\n')
    config=REPO/'.distributed.env'
    if not config.exists():
        private_env(config,{'VIDEO_USE_DISTRIBUTED':'1',
            'VIDEO_USE_DATABASE_URL':f'postgresql://video_use:{secrets.token_hex(24)}@127.0.0.1:{DB_PORT}/video_use',
            'VIDEO_USE_SESSION_SECRET':secrets.token_hex(32),'VIDEO_USE_CANCEL_POLL_SECONDS':'.5',
            'VIDEO_USE_DRAIN_SECONDS':'30','VIDEO_USE_CONCURRENCY':'1','VIDEO_USE_NODE_CONCURRENCY':'1',
            'VIDEO_USE_USER_CONCURRENCY':'2','VIDEO_USE_NODE_CPU_SLOTS':'3','VIDEO_USE_NODE_MEMORY_MB':'12288',
            'VIDEO_USE_TASK_MEMORY_BYTES':str(12*1024**3),'VIDEO_USE_TASK_DISK_BYTES':str(2*1024**3),
            'VIDEO_USE_GPU_LEASE_ROOT':'/data/wujing03/.media-gpu-leases','VIDEO_USE_WORKSPACE_STORAGE':'disk'})
    settings=env_file(config)
    gpu_candidates={node:node_gpu_devices(settings,node) for node in PORTS}
    postgres=pwd.getpwnam('postgres');service=pwd.getpwnam('video-use')
    for path in (ROOT,REPO,REPO.parent):run(['setfacl','-m','u:postgres:--x',path])
    run(['setfacl','-m','u:video-use:--x',ROOT])
    pg=ROOT/'postgres';pg.mkdir(mode=0o700,exist_ok=True);os.chown(pg,postgres.pw_uid,postgres.pw_gid)
    if not (pg/'PG_VERSION').exists():
        run(['runuser','-u','postgres','--',PG_BIN/'initdb','-D',pg,'--auth-local=peer','--auth-host=scram-sha-256','--no-locale','--encoding=UTF8'],timeout=60)
        with (pg/'postgresql.conf').open('a') as stream:stream.write(f"\nlisten_addresses='127.0.0.1'\nport={DB_PORT}\nunix_socket_directories='/run/video-use-db'\nmax_connections=100\nshared_buffers='256MB'\npassword_encryption='scram-sha-256'\n")
    for node,port in PORTS.items():
        for role in ('api','node'):
            path=data(role,node);path.mkdir(mode=0o700,exist_ok=True)
            if role=='api':os.chown(path,service.pw_uid,service.pw_gid)
            (path/'tmp').mkdir(mode=0o700,exist_ok=True)
            if role=='api':os.chown(path/'tmp',service.pw_uid,service.pw_gid)
        registry(node).mkdir(mode=0o700,parents=True,exist_ok=True)
        (ROOT/('media-'+node)/'tmp').mkdir(mode=0o700,parents=True,exist_ok=True)
        private_env(CONF/f'media-{node}.env',{'TMPDIR':ROOT/('media-'+node)/'tmp'})
        private_env(CONF/f'api-{node}.env',{'VIDEO_USE_DATA_DIR':data('api',node),'TMPDIR':data('api',node)/'tmp','VIDEO_USE_API_PORT':port,
                                         'VIDEO_USE_MEDIA_SOCKET':f'/run/vu-media-{node}/decoder.sock'})
        private_env(CONF/f'node-{node}.env',{'VIDEO_USE_DATA_DIR':data('node',node),'TMPDIR':data('node',node)/'tmp',
            'VIDEO_USE_EXECUTOR_ROOT':registry(node),'VIDEO_USE_UID_START':UIDS[node],'VIDEO_USE_UID_COUNT':64,
            'VIDEO_USE_GPU_DEVICES':gpu_candidates[node],'VIDEO_USE_NODE_OPERATIONS':'*'})
    for name,text in {'video-use-api@.service':api_unit(),'video-use-executor@.service':worker_unit(),
                      'video-use-watchdog@.service':reaper_unit(),'video-use-media@.service':media_unit(),OBS_SERVICE:observer_unit(),DB_SERVICE:postgres_unit()}.items():
        (Path('/etc/systemd/system')/name).write_text(text)
    for node in PORTS:
        other='b' if node=='a' else 'a'
        for role in ('api','executor','watchdog'):
            path=Path('/etc/systemd/system')/f'video-use-{role}@{node}.service.d';path.mkdir(exist_ok=True)
            hidden=[data('node',other),registry(other),data('api','a'),data('api','b')]
            if role=='api':hidden=[data('node','a'),data('node','b'),data('api',other),registry('a'),registry('b')]
            (path/'isolation.conf').write_text('[Service]\nInaccessiblePaths='+' '.join(map(str,hidden))+'\n')
    run(['systemctl','daemon-reload'])


def initialize_database():
    run(['systemctl','start',DB_SERVICE])
    for _ in range(30):
        if run(['runuser','-u','postgres','--',PG_BIN/'pg_isready','-h','/run/video-use-db','-p',str(DB_PORT)],check=False).returncode==0:break
        time.sleep(1)
    else:raise RuntimeError('Dedicated PostgreSQL did not become ready')
    from urllib.parse import urlsplit
    values=env_file(REPO/'.distributed.env');password=urlsplit(values['VIDEO_USE_DATABASE_URL']).password
    # The generated password is hexadecimal; values never appear in argv or output.
    if not password or any(c not in '0123456789abcdef' for c in password):raise ValueError('Unexpected database credential encoding')
    sql=f"SELECT 'CREATE ROLE video_use LOGIN PASSWORD ''{password}''' WHERE NOT EXISTS(SELECT 1 FROM pg_roles WHERE rolname='video_use')\n\\gexec\nSELECT 'CREATE DATABASE video_use OWNER video_use' WHERE NOT EXISTS(SELECT 1 FROM pg_database WHERE datname='video_use')\n\\gexec\n"
    run(['runuser','-u','postgres','--',PG_BIN/'psql','-X','-v','ON_ERROR_STOP=1','-h','/run/video-use-db','-p',str(DB_PORT),'-d','postgres'],input=sql)
    subprocess.run([RELEASE/'.venv/bin/python','-c','import webapp.app'],cwd=RELEASE,env=environment(),check=True,timeout=60,capture_output=True)


def install_examples():
    source=REPO/'workspace/demos'
    result=run([RELEASE/'.venv/bin/python','-m','webapp.demo_library','--source',source],
               cwd=RELEASE,env={**environment(),'PYTHONPATH':str(RELEASE),'VIDEO_USE_TELEMETRY_STREAM':'stderr'},timeout=900)
    report=json.loads(result.stdout.strip().splitlines()[-1])
    if not report.get('examples'):raise RuntimeError('Shared example library is empty')
    print(f"Shared example library ready: {report['examples']} examples, {report['files']} files.",flush=True)


def wait_clean():
    for _ in range(60):
        if not any(list((registry(n)/'records').glob('*.json')) for n in PORTS):return
        time.sleep(1)
    raise RuntimeError('Local cleanup remains pending; watchdogs and database left running for recovery')


def stop():
    if (ROOT/'observability').exists():(ROOT/'observability/maintenance').touch(mode=0o600)
    if PROXY.exists():proxy(maintenance=True)
    parallel_service('stop',[s for s in APIS+WORKERS if active(s)])
    if active(DB_SERVICE):wait_clean()
    parallel_service('stop',[s for s in REAPERS+MEDIA if active(s)])
    if active(DB_SERVICE):run(['systemctl','stop',DB_SERVICE])
    print('Cluster stopped; PostgreSQL files, BOSS objects and private configuration retained.',flush=True)


def start():
    if not (CONF/'node-a.env').exists():raise RuntimeError('Run ./deploy.sh deploy first')
    run(['systemctl','start',OBS_SERVICE])
    run(['systemctl','start',DB_SERVICE]);parallel_service('start',REAPERS+MEDIA)
    parallel_service('start',APIS+WORKERS)
    health()
    proxy()
    (ROOT/'observability/maintenance').unlink(missing_ok=True)
    print('Cluster ready: /video-use/ (API a + b; executor a + b).',flush=True)


def health():
    import requests
    client=requests.Session();client.trust_env=False
    for _ in range(45):
        try:
            results=[client.get(f'http://127.0.0.1:{port}/api/agent/health',timeout=2) for port in PORTS.values()]
            if all(r.ok and r.json().get('available') for r in results):
                public_files=['chat.js','task_progress.js','common.js','activity.js','studio.css']
                assets=[client.get(f'http://127.0.0.1:{port}/static/{name}',timeout=2) for port in PORTS.values() for name in public_files]
                if all(r.ok and r.content for r in assets):
                    demos=[client.get(f'http://127.0.0.1:{port}/api/demos',timeout=5) for port in PORTS.values()]
                    if all(r.ok and isinstance(r.json(),list) and r.json() for r in demos):
                        previews=[client.get(f"http://127.0.0.1:{port}/api/demos/{r.json()[0]['id']}/files/{r.json()[0]['poster']}",timeout=10) for port,r in zip(PORTS.values(),demos)]
                        if all(r.ok and r.content for r in previews):return
        except requests.RequestException:pass
        time.sleep(1)
    raise RuntimeError('API or executor health did not become available')


def status():
    names=[DB_SERVICE,*APIS,*WORKERS,*REAPERS,*MEDIA,OBS_SERVICE]
    print(json.dumps({'services':{name:active(name) for name in names},'api_ports':PORTS,
        'entrypoint':'/video-use/','database_port':DB_PORT,'data_root':str(ROOT)},indent=2))


def deploy():
    stage_release();backup()
    examples_ready=active(DB_SERVICE) and (CONF/'node-a.env').exists()
    if examples_ready:install_examples()
    if any(active(s) for s in APIS+WORKERS+REAPERS):stop()
    if active(LEGACY[0]):
        with sqlite3.connect('file:'+str(REPO/'workspace/state.sqlite3')+'?mode=ro',uri=True) as db:
            count=db.execute("SELECT COUNT(*) FROM agent_turns WHERE status IN ('queued','running','waiting','paused','cancelling')").fetchone()[0]
            count+=db.execute("SELECT COUNT(*) FROM jobs WHERE status IN ('queued','running')").fetchone()[0]
        if count:raise RuntimeError('Legacy deployment still has unfinished tasks; finish or cancel those before switching databases')
    parallel_service('stop',[s for s in LEGACY[:2] if active(s)])
    if active(LEGACY[2]):run(['systemctl','stop',LEGACY[2]])
    configure()
    initialize_database()
    if not examples_ready:install_examples()
    run(['systemctl','enable',DB_SERVICE,*APIS,*WORKERS,*REAPERS,*MEDIA,OBS_SERVICE])
    if active(OBS_SERVICE):run(['systemctl','restart',OBS_SERVICE])
    run(['systemctl','disable',*LEGACY])
    start()
    (ROOT/'release.json').write_text(json.dumps({'source':str(RELEASE),'sha256':digest(RELEASE),'deployed_at':time.time()},indent=2)+'\n')


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action',choices=['deploy','start','stop','restart','status','check'],nargs='?',default='status')
    args=parser.parse_args();os.chdir(REPO)
    if os.geteuid()!=0:raise SystemExit('Run this service management script as root.')
    with open('/run/video-use-cluster-control.lock','a') as lock:
        fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
        if args.action=='restart':stop();start()
        elif args.action=='check':
            source=Path(json.loads((ROOT/'release.json').read_text())['source']) if (ROOT/'release.json').exists() else REPO
            gate(source);print('Release test and private configuration checks passed.')
        else:globals()[args.action]()


if __name__=='__main__':
    try:main()
    except Exception as exc:
        print('Cluster action failed: '+str(exc),file=sys.stderr)
        raise SystemExit(1)
