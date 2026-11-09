"""Sandbox-side checkpoint export and Executor-side verified collection."""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path,PurePosixPath
import stat


class RuntimeWorkspaceError(ValueError):pass


class WorkspaceExporter:
    def __init__(self,workspace='/workspace',state='/state',*,max_files=50000,max_bytes=8*1024**3):
        self.roots={'workspace':Path(workspace),'state':Path(state)}
        self.max_files=max_files;self.max_bytes=max_bytes

    def export(self):
        manifest={};count=total=0
        for group,root in self.roots.items():
            records={}
            if not root.exists():manifest[group]=records;continue
            for base,dirs,names in os.walk(root,followlinks=False):
                base=Path(base)
                if group=='workspace':
                    from .sandbox_path_policy import is_reserved_scratch_path
                    dirs[:]=[name for name in dirs
                        if not is_reserved_scratch_path((base/name).relative_to(root).as_posix())]
                if any((base/name).is_symlink() for name in dirs):raise RuntimeWorkspaceError('Workspace contains a linked directory')
                for name in names:
                    path=base/name;relative=path.relative_to(root).as_posix()
                    if group=='workspace' and not relative.startswith('edit/'):continue
                    if group=='workspace' and is_reserved_scratch_path(relative):continue
                    if group=='state':
                        from .session_checkpoint import persistent
                        if not persistent(relative):continue
                    try:fd=os.open(path,os.O_RDONLY|os.O_CLOEXEC|os.O_NOFOLLOW)
                    except OSError as exc:raise RuntimeWorkspaceError('Workspace file cannot be opened safely') from exc
                    try:
                        info=os.fstat(fd)
                        if not stat.S_ISREG(info.st_mode):raise RuntimeWorkspaceError('Workspace entry is not a regular file')
                        with os.fdopen(os.dup(fd),'rb') as stream:digest=hashlib.file_digest(stream,'sha256').hexdigest()
                    finally:os.close(fd)
                    count+=1;total+=info.st_size
                    if count>self.max_files or total>self.max_bytes:raise RuntimeWorkspaceError('Workspace export exceeds limits')
                    record={'size':info.st_size,'sha256':digest,'executable':bool(info.st_mode&0o100)}
                    records[relative]=record
            manifest[group]=records
        return manifest

    def export_descriptor(self,path='/run/video-use/export-manifest.json'):
        target=Path(path);data=json.dumps(self.export(),sort_keys=True,separators=(',',':')).encode()
        temporary=target.with_name(target.name+'.pending')
        temporary.write_bytes(data);temporary.chmod(0o600);temporary.replace(target)
        return {'path':str(target),'size':len(data),'sha256':hashlib.sha256(data).hexdigest()}


class WorkspaceCollector:
    def __init__(self,files,*,max_files=50000,max_bytes=8*1024**3):
        self.files=files;self.max_files=max_files;self.max_bytes=max_bytes

    def collect(self,manifest,sink):
        count=total=0
        if not isinstance(manifest,dict) or set(manifest)-{'workspace','state'}:raise RuntimeWorkspaceError('Invalid exported manifest')
        for group in ('workspace','state'):
            records=manifest.get(group,{})
            if not isinstance(records,dict):raise RuntimeWorkspaceError('Invalid exported group')
            for name,record in records.items():
                path=PurePosixPath(name)
                if path.is_absolute() or '..' in path.parts or not name:raise RuntimeWorkspaceError('Exported path escapes root')
                if not isinstance(record,dict) or set(record)!={'size','sha256','executable'}:raise RuntimeWorkspaceError('Invalid exported record')
                count+=1;total+=record['size']
                if count>self.max_files or total>self.max_bytes:raise RuntimeWorkspaceError('Collected workspace exceeds limits')
                data=self.files.read('/'+group+'/'+path.as_posix())
                if len(data)!=record['size'] or hashlib.sha256(data).hexdigest()!=record['sha256']:
                    raise RuntimeWorkspaceError('Collected workspace integrity mismatch')
                sink(group,path.as_posix(),data,record)
        return {'files':count,'bytes':total}
