"""Private API file preparation and short, cancellation-aware publication."""
import asyncio
from pathlib import Path
import shutil
import threading
import time
import uuid
import os
import stat
import anyio
from .api_execution import APIWorkCancelled,check_cancelled


def text_file(store,project,relative,limit=16*1024**2):
    if store.shared_files and store.artifacts.resolve(project,relative)['size']>limit:
        raise OverflowError('Text source exceeds editor size limit')
    path=store.file(project,relative)
    descriptor=os.open(path,os.O_RDONLY|os.O_NOFOLLOW|os.O_NONBLOCK)
    with os.fdopen(descriptor,'rb') as stream:
        info=os.fstat(stream.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_nlink!=1:raise ValueError('Text source must be a regular private file')
        raw=stream.read(limit+1)
    if len(raw)>limit:raise OverflowError('Text source exceeds editor size limit')
    return raw.decode('utf-8-sig')


class Staging:
    def __init__(self,root):
        self.path=Path(root)/'api-incoming'/uuid.uuid4().hex
        self.lock=threading.RLock();self.aborted=threading.Event()
        self.committed=False;self.published=[]

    def check(self):
        check_cancelled()
        if self.aborted.is_set():raise APIWorkCancelled('API file operation was abandoned')

    def open_output(self,name):
        if Path(name).name!=name:raise ValueError('Invalid staging name')
        with self.lock:
            self.check();self.path.mkdir(parents=True,exist_ok=True,mode=0o700)
            return (self.path/name).open('xb')

    def copy(self,source,name,limit):
        size=0
        with self.open_output(name) as output:
            while chunk:=source.read(1024*1024):
                self.check();size+=len(chunk)
                if size>limit:raise OverflowError('Upload size limit exceeded')
                output.write(chunk)
        return size

    def write(self,name,data):
        with self.open_output(name) as output:self.check();output.write(data)
        return self.path/name

    def move(self,name,destination):
        with self.lock:
            self.check();destination=Path(destination);self.published.append(destination)
            destination.parent.mkdir(parents=True,exist_ok=True)
            (self.path/name).replace(destination)

    def commit(self,function):
        # Cleanup cannot race the database commit and remove a published file.
        # This is a private request lock, never the application mutation lock.
        with self.lock:
            self.check();value=function();self.committed=True;return value

    def cleanup(self):
        self.aborted.set()
        with self.lock:
            shutil.rmtree(self.path,ignore_errors=True)
            if not self.committed:
                for path in self.published:path.unlink(missing_ok=True)

    async def finish(self,executors):
        self.aborted.set()
        # Do not hold the request indefinitely for cleanup. An executing
        # cleanup still owns its worker; old private directories are swept too.
        with anyio.CancelScope(shield=True):
            try:
                async with asyncio.timeout(2):await executors.run('cleanup',self.cleanup)
            except Exception as exc:
                from .telemetry import emit
                emit('api.cleanup_deferred','WARNING',exception_type=type(exc).__name__)


def old_directories(folder,older_than,cursor=''):
    import heapq,os
    folder=Path(folder)
    if folder.is_symlink():raise ValueError('Cache cleanup root must not be a link')
    if not folder.exists():return [],''
    before=time.time()-older_than
    def eligible():
        with os.scandir(folder) as entries:
            for entry in entries:
                check_cancelled()
                if entry.name<=cursor or len(entry.name)!=32 or any(c not in '0123456789abcdef' for c in entry.name):continue
                try:
                    if entry.is_dir(follow_symlinks=False) and entry.stat(follow_symlinks=False).st_mtime<before:yield entry.name
                except FileNotFoundError:continue
    names=heapq.nsmallest(1000,eligible())
    return names,names[-1] if len(names)==1000 else ''


def sweep(root,older_than=3600,state=None):
    state=state if state is not None else {}
    folder=Path(root)/'api-incoming';names,cursor=old_directories(folder,older_than,state.get('incoming',''))
    removed=0
    for name in names:
        check_cancelled()
        try:shutil.rmtree(folder/name);removed+=1
        except FileNotFoundError:pass
    state['incoming']=cursor
    return removed
