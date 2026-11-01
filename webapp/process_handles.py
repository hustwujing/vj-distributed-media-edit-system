"""pidfd support even when Python was built against older libc headers."""
import ctypes
import os
import platform
import signal


def syscall(number,*args):
    if platform.machine() not in ('x86_64','aarch64'):raise RuntimeError('A supported pidfd binding is required on this architecture')
    libc=ctypes.CDLL(None,use_errno=True);libc.syscall.restype=ctypes.c_long
    value=libc.syscall(ctypes.c_long(number),*args)
    if value<0:
        error=ctypes.get_errno()
        raise OSError(error,os.strerror(error))
    return value


def open_pid(pid):
    if hasattr(os,'pidfd_open'):return os.pidfd_open(pid)
    return syscall(434,ctypes.c_int(pid),ctypes.c_uint(0))


def send_signal(fd,sig):
    if hasattr(signal,'pidfd_send_signal'):return signal.pidfd_send_signal(fd,sig)
    return syscall(424,ctypes.c_int(fd),ctypes.c_int(sig),ctypes.c_void_p(),ctypes.c_uint(0))
