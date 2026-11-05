"""Application data stays in the workspace explicitly selected by the user."""
import os
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
DEFAULT_DATA_DIR = REPO / 'workspace'
SANDBOX_WORKSPACE = '/workspace'


def data_root() -> Path:
    return Path(os.environ.get('VIDEO_USE_DATA_DIR', DEFAULT_DATA_DIR)).expanduser().resolve()


def temporary_socket(directory) -> Path:
    """Keep attempt-local AF_UNIX names short on data-disk installations."""
    import uuid
    root=Path(os.environ.get('VIDEO_USE_SOCKET_ROOT',directory))
    root.mkdir(parents=True,exist_ok=True)
    path=root/(uuid.uuid4().hex[:12]+'.sock')
    if len(os.fsencode(path))>=108:
        raise ValueError('Executor socket directory exceeds the Linux AF_UNIX path limit')
    return path
