"""Parameterized systemd and Nginx templates shared by deployments."""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class ServiceConfig:
    repo: Path
    root: Path
    release: Path
    conf: Path
    executors: Path
    namespace: str
    service_user: str
    database_service: str = 'video-use-db.service'
    root_path: str = '/video-use'
    agent_runtime: str = 'video-use-agent'
    media_runtime_prefix: str = 'vu-media-'
    gpu_lease_root: Path = Path('/data/wujing03/.media-gpu-leases')
    require_database: bool = True
    worker_memory_max: str = '14G'
    worker_cpu_quota: str = '300%'
    worker_tasks_max: int = 512
    observer_port: int | None = None

    def registry(self, instance='%i'):
        return self.executors / instance


def unit_header(config, description):
    database_dependency = ('Requires=' if config.require_database else 'Wants=') + config.database_service
    return f'''[Unit]
Description={description}
After=network.target {config.database_service}
{database_dependency}

[Service]
WorkingDirectory={config.release}
EnvironmentFile={config.repo}/.env
EnvironmentFile={config.repo}/.agent.env
EnvironmentFile={config.repo}/.storage.env
EnvironmentFile={config.repo}/.distributed.env
Environment=PYTHONDONTWRITEBYTECODE=1
EnvironmentFile={config.conf}/observation.env
Environment=VIDEO_USE_RELEASE={config.release.name}
Environment=VIDEO_USE_INSTANCE=%n
LogNamespace={config.namespace}
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


def _owned_dependency(header, config, service):
    after = f'After=network.target {config.database_service}'
    return header.replace(after, after + ' ' + service).replace(
        f'[Service]\n', f'Requires={service}\n\n[Service]\n', 1
    )


def api_unit(config):
    media = f'{config.namespace}-media@%i.service'
    header = _owned_dependency(unit_header(config, 'Distributed Agent API %i'), config, media)
    return header + f'''User={config.service_user}
Group={config.service_user}
EnvironmentFile={config.conf}/api-%i.env
ExecStart={config.release}/.venv/bin/python -m uvicorn webapp.app:app --host 127.0.0.1 --port ${{VIDEO_USE_API_PORT}} --root-path {config.root_path} --proxy-headers --forwarded-allow-ips 127.0.0.1
ReadWritePaths={config.root}/api-%i
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


def worker_unit(config):
    watchdog = f'{config.namespace}-watchdog@%i.service'
    header = _owned_dependency(unit_header(config, 'Distributed Agent executor %i'), config, watchdog)
    return header + f'''User=root
Group={config.service_user}
EnvironmentFile={config.conf}/node-%i.env
ExecStart={config.release}/.venv/bin/python -m webapp.agent_host
RuntimeDirectory={config.agent_runtime}
RuntimeDirectoryMode=0700
RuntimeDirectoryPreserve=yes
ReadWritePaths={config.root}/node-%i {config.registry()} /run/{config.agent_runtime} /sys/fs/cgroup {config.gpu_lease_root}
NoNewPrivileges=false
ProtectControlGroups=false
RestrictSUIDSGID=true
RestrictNamespaces=mnt pid net ipc uts cgroup
CapabilityBoundingSet=CAP_SYS_ADMIN CAP_SYS_CHROOT CAP_SETUID CAP_SETGID CAP_SETPCAP CAP_CHOWN CAP_FOWNER CAP_DAC_OVERRIDE CAP_KILL CAP_NET_ADMIN
TasksMax={config.worker_tasks_max}
MemoryMax={config.worker_memory_max}
CPUQuota={config.worker_cpu_quota}
TimeoutStopSec=90
UMask=0007

[Install]
WantedBy=multi-user.target
'''


def reaper_unit(config):
    return unit_header(config, 'Distributed Agent node watchdog %i') + f'''User=root
EnvironmentFile={config.conf}/node-%i.env
ExecStart={config.release}/.venv/bin/python -m webapp.execution_reaper
RuntimeDirectory={config.agent_runtime}
RuntimeDirectoryMode=0700
RuntimeDirectoryPreserve=yes
ReadWritePaths={config.root}/node-%i {config.registry()} /run/{config.agent_runtime} /sys/fs/cgroup
CapabilityBoundingSet=CAP_SYS_ADMIN CAP_SYS_CHROOT CAP_SYS_PTRACE CAP_KILL CAP_DAC_OVERRIDE CAP_FOWNER CAP_CHOWN CAP_SETUID CAP_SETGID CAP_SETPCAP
TasksMax=256
MemoryMax=768M
CPUQuota=100%
TimeoutStopSec=30
UMask=0077

[Install]
WantedBy=multi-user.target
'''


def media_unit(config):
    return f'''[Unit]
Description=Distributed Agent fixed media decoder %i
After=local-fs.target

[Service]
User=root
Group={config.service_user}
WorkingDirectory={config.release}
EnvironmentFile={config.conf}/api-%i.env
Environment=VIDEO_USE_TELEMETRY_ENABLED=1
Environment=VIDEO_USE_RELEASE={config.release.name}
Environment=VIDEO_USE_INSTANCE=%n
LogNamespace={config.namespace}
Environment=PYTHONDONTWRITEBYTECODE=1
EnvironmentFile={config.conf}/media-%i.env
RuntimeDirectory={config.media_runtime_prefix}%i
RuntimeDirectoryMode=0750
ExecStart={config.release}/.venv/bin/python -m webapp.media_service
Restart=on-failure
RestartSec=2
KillMode=control-group
TimeoutStopSec=50
ProtectSystem=strict
ProtectHome=true
PrivateTmp=true
ReadWritePaths=/run/{config.media_runtime_prefix}%i {config.root}/media-%i
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


def observer_unit(config):
    port = f' --port {config.observer_port}' if config.observer_port is not None else ''
    return f'''[Unit]
Description=Distributed Agent observations and diagnostics
After=network.target systemd-journald@{config.namespace}.service

[Service]
WorkingDirectory={config.release}
EnvironmentFile={config.repo}/.env
EnvironmentFile={config.repo}/.agent.env
EnvironmentFile={config.repo}/.storage.env
EnvironmentFile={config.repo}/.distributed.env
EnvironmentFile={config.conf}/node-a.env
EnvironmentFile={config.conf}/observation.env
Environment=VIDEO_USE_SERVICE=observer
Environment=VIDEO_USE_RELEASE={config.release.name}
ExecStart={config.release}/.venv/bin/python -m webapp.observation_collector --root {config.root}/observability{port}
Restart=on-failure
RestartSec=3
ProtectSystem=strict
ProtectHome=true
PrivateTmp=true
ReadWritePaths={config.root}/observability
NoNewPrivileges=true
MemoryMax=512M
CPUQuota=100%
TasksMax=64
UMask=0077
LogNamespace={config.namespace}

[Install]
WantedBy=multi-user.target
'''


def nginx_config(*, namespace, proxy_port, api_ports, maintenance=False, retry_503=True):
    if maintenance:
        return f'''server {{ listen 127.0.0.1:{proxy_port}; location / {{ default_type application/json; return 503 '{{"error":"Service is stopped for maintenance"}}'; }} }}
'''
    upstream = namespace.replace('-', '_') + '_cluster'
    servers = '\n'.join(
        f'    server 127.0.0.1:{port} max_fails=1 fail_timeout=5s;' for port in api_ports
    )
    retry = 'error timeout http_502 http_503 http_504' if retry_503 else 'error timeout http_502 http_504'
    return f'''upstream {upstream} {{
    least_conn;
{servers}
    keepalive 16;
}}
server {{
    listen 127.0.0.1:{proxy_port};
    location / {{
        proxy_pass http://{upstream};
        proxy_http_version 1.1;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
        proxy_set_header Connection "";
        proxy_next_upstream {retry};
        proxy_connect_timeout 3s;
        proxy_read_timeout 600s;
        proxy_send_timeout 600s;
        client_max_body_size 2048m;
        proxy_request_buffering off;
        proxy_buffering off;
        add_header X-Video-Use-API $upstream_addr always;
        add_header X-Accel-Buffering no always;
    }}
}}
'''
