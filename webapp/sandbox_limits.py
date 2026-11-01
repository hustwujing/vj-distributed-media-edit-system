"""Run after UID drop, before any untrusted executable. Fail closed."""
import ctypes
import ctypes.util
import errno
import os
import resource
import sys


def restrict(media=False):
    resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
    resource.setrlimit(resource.RLIMIT_NOFILE, (1024, 1024))
    size = 32*1024**2 if media else 2*1024**3
    resource.setrlimit(resource.RLIMIT_FSIZE, (size, size))
    if media:
        resource.setrlimit(resource.RLIMIT_AS,(2*1024**3,2*1024**3))
        resource.setrlimit(resource.RLIMIT_CPU,(30,30))
    library = 'libseccomp.so.2'
    lib = ctypes.CDLL(library, use_errno=True)
    lib.seccomp_init.argtypes = [ctypes.c_uint32]
    lib.seccomp_init.restype = ctypes.c_void_p
    lib.seccomp_syscall_resolve_name.argtypes = [ctypes.c_char_p]
    lib.seccomp_syscall_resolve_name.restype = ctypes.c_int
    lib.seccomp_rule_add.argtypes = [ctypes.c_void_p, ctypes.c_uint32, ctypes.c_int, ctypes.c_uint]
    lib.seccomp_load.argtypes = [ctypes.c_void_p]
    lib.seccomp_release.argtypes = [ctypes.c_void_p]
    ctx = lib.seccomp_init(0x7fff0000)  # ALLOW; explicit dangerous operations denied.
    if not ctx:
        raise RuntimeError('Cannot allocate seccomp policy')
    try:
        blocked = ('mount umount2 pivot_root ptrace process_vm_readv process_vm_writev '
                   'bpf perf_event_open userfaultfd kexec_load kexec_file_load '
                   'init_module finit_module delete_module reboot swapon swapoff '
                   'open_by_handle_at setns unshare keyctl add_key request_key').split()
        for name in blocked + ['clone3']:
            number = lib.seccomp_syscall_resolve_name(name.encode())
            if number < 0:
                continue
            # glibc falls back to clone for ordinary threads on ENOSYS.
            action = 0x00050000 | (errno.ENOSYS if name == 'clone3' else errno.EPERM)
            if lib.seccomp_rule_add(ctx, action, number, 0) != 0:
                raise RuntimeError('Cannot build seccomp policy')
        class Comparison(ctypes.Structure):
            _fields_ = [('arg', ctypes.c_uint), ('op', ctypes.c_int),
                        ('mask', ctypes.c_uint64), ('value', ctypes.c_uint64)]
        # Keep ordinary process/thread creation, disallow nested namespaces.
        lib.seccomp_rule_add_array.argtypes = [ctypes.c_void_p, ctypes.c_uint32, ctypes.c_int,
                                              ctypes.c_uint, ctypes.POINTER(Comparison)]
        clone = lib.seccomp_syscall_resolve_name(b'clone')
        for flag in (0x00020000, 0x02000000, 0x04000000, 0x08000000,
                     0x10000000, 0x20000000, 0x40000000):
            rule = Comparison(0, 7, flag, flag)  # SCMP_CMP_MASKED_EQ
            if clone >= 0 and lib.seccomp_rule_add_array(ctx, 0x00050000 | errno.EPERM, clone, 1, ctypes.byref(rule)) != 0:
                raise RuntimeError('Cannot restrict namespace creation')
        if lib.seccomp_load(ctx) != 0:
            raise RuntimeError('Cannot install seccomp policy')
    finally:
        lib.seccomp_release(ctx)


if __name__ == '__main__':
    media = len(sys.argv)>1 and sys.argv[1]=='--media'
    if media:sys.argv.pop(1)
    restrict(media)
    os.execvp(sys.argv[1], sys.argv[1:])
