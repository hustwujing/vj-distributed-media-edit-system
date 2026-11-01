"""Small read-only decoder sandbox usable by the unprivileged API service.

Only one input file is exposed; outputs use bounded regular file descriptors,
not unbounded pipes. It has no project directories, credentials or host network.
"""
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading

SLOTS=threading.BoundedSemaphore(2)
REPO=Path(__file__).resolve().parents[1]


def run(source, arguments, timeout=45,*,input_fd=None,decode_uid=None,decode_gid=None):
    if input_fd is None:
        if source.is_symlink() or not source.is_file():raise ValueError('Invalid media source')
        if os.geteuid()!=0 and os.environ.get('VIDEO_USE_MEDIA_SOCKET'):
            from .media_service import request
            return request(source,arguments,timeout)
    prefix=Path(sys.base_prefix)
    namespace = (['--unshare-pid','--unshare-net','--unshare-ipc','--unshare-uts','--unshare-cgroup-try']
                 if os.geteuid()==0 else ['--unshare-all','--cap-drop','ALL'])
    directories={*prefix.parents,prefix,*REPO.parents,REPO}
    directories.discard(Path('/'))
    parents=[argument for path in sorted(directories,key=lambda p:(len(p.parts),str(p))) for argument in ('--perms','0755','--dir',str(path))]
    args=['/usr/bin/bwrap','--die-with-parent','--new-session',*namespace,
          '--ro-bind','/usr','/usr','--ro-bind','/lib','/lib','--ro-bind-try','/lib64','/lib64',
          '--symlink','/usr/bin','/bin','--symlink','/usr/sbin','/sbin',
          '--proc','/proc','--dev','/dev','--perms','1777','--size','67108864','--tmpfs','/tmp',
          '--perms','0755','--dir','/etc',*parents,
          '--ro-bind',str(prefix/'bin'),str(prefix/'bin'),
          '--ro-bind',str(prefix/'lib'),str(prefix/'lib'),
          '--ro-bind',str(REPO/'.venv'),str(REPO/'.venv'),
          '--ro-bind',str(REPO/'webapp/sandbox_limits.py'),'/limits.py',
          '--ro-bind',str(REPO/'webapp/media_inspect.py'),'/inspect.py',
          '--ro-bind','/etc/ld.so.cache','/etc/ld.so.cache',
          '--ro-bind','/etc/alternatives','/etc/alternatives',
          '--ro-bind','/etc/fonts','/etc/fonts',
          '--ro-bind',f'/proc/self/fd/{input_fd}' if input_fd is not None else str(source.resolve()),'/input',
          '--clearenv','--setenv','PATH','/usr/bin:/bin','--setenv','HOME','/tmp',
          '--setenv','OPENBLAS_NUM_THREADS','1','--setenv','OMP_NUM_THREADS','1']
    if os.geteuid()==0:
        info=os.fstat(input_fd) if input_fd is not None else source.stat()
        args+=['--cap-add','CAP_SETUID','--cap-add','CAP_SETGID','/usr/bin/setpriv',
               '--reuid='+str(decode_uid if decode_uid is not None else info.st_uid),'--regid='+str(decode_gid if decode_gid is not None else info.st_gid),
               '--clear-groups','--no-new-privs','--bounding-set=-all']
    args += [str(REPO/'.venv/bin/python'),'/limits.py','--media',*arguments]
    # Run as caller in a user namespace. Media bytes never reach host decoders.
    with SLOTS, tempfile.TemporaryFile() as output, tempfile.TemporaryFile() as errors:
        result=subprocess.run(args,stdout=output,stderr=errors,timeout=timeout,pass_fds=(input_fd,) if input_fd is not None else ())
        output.seek(0);errors.seek(0)
        return subprocess.CompletedProcess(arguments,result.returncode,output.read(32*1024**2),errors.read(65536))


class MediaDecoderUnavailable(RuntimeError):
    pass


def ensure_available(result):
    if result.returncode in (126,127):raise MediaDecoderUnavailable('媒体解码环境暂不可用，请凭查询编号排查。')


def inspect_image(source):
    import json
    result=run(source,[str(REPO/'.venv/bin/python'),'/inspect.py'])
    ensure_available(result)
    if result.returncode:raise ValueError('无法读取图片，请上传有效的静态 PNG/JPG/WebP 图片。')
    return json.loads(result.stdout)


def normalize_image(source):
    import struct
    result=run(source,[str(REPO/'.venv/bin/python'),'/inspect.py','--normalize'])
    ensure_available(result)
    if result.returncode or not result.stdout.startswith(b'\x89PNG\r\n\x1a\n'):
        raise ValueError('图片解码失败或超过大小限制')
    width,height=struct.unpack('>II',result.stdout[16:24])
    return result.stdout,width,height
