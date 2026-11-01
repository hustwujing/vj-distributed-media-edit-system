"""Trusted pre-bwrap trampoline: attach child to its task cgroup, then exec.

Invoked with a root-owned manifest selected by the launcher, never user input.
No preexec_fn is used in the multithreaded worker.
"""
import json
import os
import stat
import sys
import math


if __name__ == '__main__':
    descriptor = os.open(sys.argv[1], os.O_RDONLY | os.O_NOFOLLOW)
    with os.fdopen(descriptor) as stream:
        info = os.fstat(stream.fileno())
        if info.st_uid != 0 or info.st_mode & 0o022 or not stat.S_ISREG(info.st_mode):
            raise RuntimeError('Untrusted execution manifest')
        config = json.load(stream)
    for path in config['cgroups']:
        with open(path + '/cgroup.procs', 'w') as stream:
            stream.write(str(os.getpid()))
    if hasattr(os,'sched_getaffinity'):
        available=sorted(os.sched_getaffinity(0))
        count=max(1,min(len(available),math.ceil(config.get('cpus',2))))
        offset=config.get('placement',0)%len(available)
        os.sched_setaffinity(0,{available[(offset+i)%len(available)] for i in range(count)})
    os.execv(sys.argv[2], sys.argv[2:])
