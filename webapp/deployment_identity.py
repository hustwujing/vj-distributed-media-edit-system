"""Trusted deployment identities shared by launchers, gateways and reapers."""
import os
from pathlib import Path
import re


def namespace():
    value=os.environ.get('VIDEO_USE_DEPLOYMENT','video-use')
    if not re.fullmatch(r'[a-z][a-z0-9-]{0,30}',value):
        raise ValueError('Invalid deployment namespace')
    return value


def service_user():
    return namespace()


def runtime_group():
    return namespace()+'-runtime'


def socket_root():
    instance=os.environ.get('VIDEO_USE_NODE_INSTANCE','')
    if instance and not re.fullmatch(r'[a-z0-9]{1,16}',instance):raise ValueError('Invalid node instance')
    return Path('/run')/(namespace()+'-agent'+('-'+instance if instance else ''))


def cgroup_member(path):
    """Return the kernel membership path only inside this deployment."""
    path=Path(path)
    for base in (Path('/sys/fs/cgroup'),*(Path('/sys/fs/cgroup')/c for c in ('cpu','memory','pids'))):
        root=base/namespace()
        if path.is_relative_to(root) and path!=root:
            relative=path.relative_to(root)
            if '..' in relative.parts:break
            return '/'+namespace()+'/'+relative.as_posix()
    raise ValueError('Cgroup is outside this deployment')
