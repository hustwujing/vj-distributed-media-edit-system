"""Grant service read access to the resolved runtime, never the release's secrets."""
from pathlib import Path
import subprocess
import sys


def grant(venv,users=None):
    from webapp.deployment_identity import service_user,runtime_group
    target=Path(venv).resolve(strict=True)
    if not target.is_dir() or target==Path('/'):raise ValueError('Invalid virtual environment directory')
    users=tuple(users or (service_user(),))
    import pwd
    for user in users:pwd.getpwnam(user)
    traversal=','.join([*(f'u:{user}:--x' for user in users),f'g:{runtime_group()}:--x'])
    readable=','.join([*(f'u:{user}:rX' for user in users),f'g:{runtime_group()}:rX'])
    directories=(target,Path(sys.base_prefix)/'bin',Path(sys.base_prefix)/'lib')
    parents={p for directory in directories for p in directory.parents if p!=Path('/')}
    for parent in sorted(parents):subprocess.run(['setfacl','-m',traversal,str(parent)],check=True)
    for directory in directories:subprocess.run(['setfacl','-R','-m',readable,str(directory)],check=True)
    return target


if __name__=='__main__':grant(Path('.venv'))
