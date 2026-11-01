"""Grant service read access to the resolved runtime, never the release's secrets."""
from pathlib import Path
import subprocess


def grant(venv):
    target=Path(venv).resolve(strict=True)
    if not target.is_dir() or target==Path('/'):raise ValueError('Invalid virtual environment directory')
    subprocess.run(['setfacl','-R','-m','u:video-use:rX,g:video-use-runtime:rX',str(target)],check=True)
    return target


if __name__=='__main__':grant(Path('.venv'))
