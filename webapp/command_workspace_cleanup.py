"""Bounded node-local cleanup of abandoned OpenSandbox command workspaces."""
import fcntl
import json
import os
from pathlib import Path
import shutil
import sys

from .api_staging import old_directories
from .sandbox_backends import OpenSandboxCleanupRegistry


def sweep(store,cursor='',older_than=300):
    root=store.root/'command-attempts'
    names,cursor=old_directories(root,older_than,cursor)
    if not names:return {'removed':0,'cursor':cursor}
    directory=os.open(root,os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW)
    removed=0
    try:
        for attempt in names:
            try:child=os.open(attempt,os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW,dir_fd=directory)
            except FileNotFoundError:continue
            try:
                owner=os.open('.owner',os.O_CREAT|os.O_RDWR|os.O_NOFOLLOW,0o600,dir_fd=child)
                try:
                    try:fcntl.flock(owner,fcntl.LOCK_EX|fcntl.LOCK_NB)
                    except BlockingIOError:continue
                    # Unknown DB state fails closed. A dead lease alone is not
                    # evidence that remote/local cleanup has finished.
                    if store.query('SELECT attempt FROM execution_leases WHERE attempt=? AND active=1 AND expires>?',
                                   (attempt,store.now())):continue
                    if store.query("SELECT attempt FROM execution_cleanup WHERE attempt=? AND state<>'complete'",(attempt,)):continue
                    external=store.query('SELECT state FROM execution_external_cleanup WHERE id=?',
                                         (OpenSandboxCleanupRegistry.key(attempt),))
                    if external:
                        if external[0]['state']!='complete':continue
                    elif store.query('SELECT id FROM execution_attempts WHERE id=?',(attempt,)):
                        continue
                    # With no attempt row at all, project deletion has already
                    # removed its durable identity; the grace period and owner
                    # lock still protect in-progress local preparation.
                    shutil.rmtree(attempt,dir_fd=directory);removed+=1
                finally:os.close(owner)
            finally:os.close(child)
    finally:os.close(directory)
    return {'removed':removed,'cursor':cursor}


if __name__=='__main__':
    from .store import Store
    store=Store(Path(sys.argv[1]),timeout=1,initialize_schema=False)
    print(json.dumps(sweep(store,sys.argv[2] if len(sys.argv)>2 else '')))
