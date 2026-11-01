"""Application data stays in the workspace explicitly selected by the user."""
import os
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
DEFAULT_DATA_DIR = REPO / 'workspace'
SANDBOX_WORKSPACE = '/workspace'


def data_root() -> Path:
    return Path(os.environ.get('VIDEO_USE_DATA_DIR', DEFAULT_DATA_DIR)).expanduser().resolve()
