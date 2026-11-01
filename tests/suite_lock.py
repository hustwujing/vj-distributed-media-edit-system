"""Serialize independent privileged suites sharing host test UID ranges."""
from contextlib import contextmanager
import fcntl
from functools import wraps
import os
import stat


@contextmanager
def suite_lock():
    path=os.environ.get('VIDEO_USE_TEST_SUITE_LOCK',f'/tmp/video-use-test-suite-{os.geteuid()}.lock')
    descriptor=os.open(path,os.O_RDWR|os.O_CREAT|os.O_NOFOLLOW,0o600)
    try:
        info=os.fstat(descriptor)
        if info.st_uid!=os.geteuid() or not stat.S_ISREG(info.st_mode):raise RuntimeError('Unsafe suite lock')
        try:fcntl.flock(descriptor,fcntl.LOCK_EX|fcntl.LOCK_NB)
        except BlockingIOError:
            print('Waiting for the other privileged test suite to release its host UID ranges.',flush=True)
            fcntl.flock(descriptor,fcntl.LOCK_EX)
        yield
    finally:os.close(descriptor)


def serialized(function):
    @wraps(function)
    def run(*args,**kwargs):
        with suite_lock():return function(*args,**kwargs)
    return run
