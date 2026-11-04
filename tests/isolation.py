"""Keep ordinary regression suites away from deployment credentials and services."""
import ctypes
import fcntl
import os
from pathlib import Path
import socket
import struct
import sys
import tempfile


def temporary_root():
    """Test fixtures must not write into an installed release's workspace."""
    root=Path(os.environ.get('VIDEO_USE_TEST_TMP_ROOT') or os.environ.get('TMPDIR') or Path.cwd().parent/'vu-tmp')
    root.mkdir(parents=True,exist_ok=True)
    return root


def isolate_environment(root, runtime_build=None):
    root=Path(root)
    keep={'VIDEO_USE_TEST_REAL_MODELS','VIDEO_USE_TEST_SUITE_LOCK','VIDEO_USE_WORKSPACE_STORAGE',
          'VIDEO_USE_GPU_MANAGER_PATH','VIDEO_USE_GPU_LEASE_ROOT','VIDEO_USE_GPU_DEVICES','VIDEO_USE_CODEX_BIN'}
    for name in list(os.environ):
        upper=name.upper()
        external=(name.startswith(('VIDEO_USE_','AWS_','PG')) or upper in {
            'ELEVENLABS_API_KEY','OPENAI_API_KEY','ANTHROPIC_API_KEY',
            'HTTP_PROXY','HTTPS_PROXY','ALL_PROXY','NO_PROXY'})
        if external and name not in keep:os.environ.pop(name,None)
    task_tmp=root/'tmp';task_tmp.mkdir(mode=0o755,parents=True,exist_ok=True)
    shared_tmp=Path(__file__).resolve().parents[2]/'t';shared_tmp.mkdir(mode=0o755,exist_ok=True);shared_tmp.chmod(0o1777)
    os.environ.update(VIDEO_USE_DATA_DIR=str(root/'data'),VIDEO_USE_EXECUTOR_ROOT=str(root/'executors'),VIDEO_USE_TEST_TMP_ROOT=str(task_tmp),
                      TMPDIR=str(shared_tmp),TMP=str(shared_tmp),TEMP=str(shared_tmp),
                      VIDEO_USE_UID_START='800000',VIDEO_USE_UID_COUNT='128',
                      AWS_EC2_METADATA_DISABLED='true',AWS_CONFIG_FILE='/dev/null',AWS_SHARED_CREDENTIALS_FILE='/dev/null')
    # tempfile may have cached /tmp before the suite installed isolation.
    tempfile.tempdir=str(shared_tmp)
    if runtime_build is not None:os.environ['VIDEO_USE_RUNTIME_BUILD']=str(runtime_build)


def isolate_network():
    """Real fixtures use loopback; live storage has a separate explicit runner."""
    if sys.platform!='linux' or os.geteuid()!=0:
        os.environ['VIDEO_USE_TEST_NETWORK_ISOLATED']='0'
        return
    libc=ctypes.CDLL(None,use_errno=True)
    if libc.unshare(0x40000000)!=0:
        raise OSError(ctypes.get_errno(),'Privileged regression requires an isolated network namespace')
    with socket.socket(socket.AF_INET,socket.SOCK_DGRAM) as control:
        request=struct.pack('16sH22x',b'lo',0)
        flags=struct.unpack_from('H',fcntl.ioctl(control.fileno(),0x8913,request),16)[0]
        fcntl.ioctl(control.fileno(),0x8914,struct.pack('16sH22x',b'lo',flags|1))
    os.environ['VIDEO_USE_TEST_NETWORK_ISOLATED']='1'
