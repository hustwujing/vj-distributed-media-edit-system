"""Entrypoint INSIDE the already isolated filesystem/network/PID namespaces."""
import subprocess
import time
import socket
import os
import sys

# The sandbox has no host network. This forwards only to its own narrow gateway.
bridge = subprocess.Popen(['/usr/bin/socat', 'TCP-LISTEN:18766,bind=127.0.0.1,reuseaddr,fork',
                           'UNIX-CONNECT:/gateway/model.sock'], stdin=subprocess.DEVNULL,
                          stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
for _ in range(40):
    try:
        with socket.create_connection(('127.0.0.1', 18766), timeout=.2):
            break
    except OSError:
        time.sleep(.05)
else:
    raise SystemExit('Isolated model gateway could not start')
if sys.argv[1]=='--command':
    os.execv(sys.argv[2],sys.argv[2:])
else:
    os.execv(sys.argv[1], [sys.argv[1], 'app-server', '--listen', 'stdio://'])
