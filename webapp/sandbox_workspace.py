"""Restore an authoritative Artifact manifest into a sandbox workspace."""
from __future__ import annotations

import hashlib
import os
from pathlib import PurePosixPath
import stat


class WorkspaceManifestError(ValueError):
    pass


class SandboxWorkspaceRestorer:
    def __init__(self,resolver,*,max_files=50000,max_bytes=8*1024**3):
        self.resolver=resolver;self.max_files=max_files;self.max_bytes=max_bytes

    @staticmethod
    def _relative(value):
        if not isinstance(value,str) or not value or '\x00' in value:
            raise WorkspaceManifestError('Invalid workspace path')
        path=PurePosixPath(value)
        if path.is_absolute() or '..' in path.parts or '.' in path.parts:
            raise WorkspaceManifestError('Workspace path escapes its root')
        return path

    def validate(self,manifest):
        if not isinstance(manifest,dict) or set(manifest)-{'workspace','state'}:
            raise WorkspaceManifestError('Invalid workspace manifest')
        files=[];total=0
        for group in ('workspace','state'):
            records=manifest.get(group,{})
            if not isinstance(records,dict):raise WorkspaceManifestError('Invalid workspace file group')
            for name,record in records.items():
                relative=self._relative(name)
                if (not isinstance(record,dict) or set(record)-{'artifact','size','sha256','executable'} or
                        not isinstance(record.get('artifact'),str) or
                        not isinstance(record.get('size'),int) or record['size']<0 or
                        not isinstance(record.get('sha256'),str) or len(record['sha256'])!=64 or
                        type(record.get('executable',False)) is not bool):
                    raise WorkspaceManifestError('Invalid workspace artifact record')
                total+=record['size'];files.append((group,relative,record))
        if len(files)>self.max_files or total>self.max_bytes:
            raise WorkspaceManifestError('Workspace manifest exceeds limits')
        return files

    @staticmethod
    def _verify(path,record):
        try:fd=os.open(path,os.O_RDONLY|os.O_CLOEXEC|os.O_NOFOLLOW)
        except OSError as exc:raise WorkspaceManifestError('Materialized artifact cannot be opened safely') from exc
        stream=os.fdopen(fd,'rb')
        try:
            info=os.fstat(fd)
            if not stat.S_ISREG(info.st_mode) or info.st_size!=record['size']:
                raise WorkspaceManifestError('Materialized artifact type or size mismatch')
            digest=hashlib.file_digest(stream,'sha256').hexdigest()
            if digest!=record['sha256']:
                raise WorkspaceManifestError('Materialized artifact digest mismatch')
            stream.seek(0);return stream
        except Exception:
            stream.close();raise

    def restore(self,files,manifest):
        # Verify every source before making the first sandbox mutation.
        prepared=[]
        try:
            for group,relative,record in self.validate(manifest):
                prepared.append((group,relative,record,self._verify(self.resolver(record['artifact']),record)))
            directories=set()
            for group,relative,_,_ in prepared:
                parent=relative.parent
                while str(parent)!='.':directories.add('/'+group+'/'+parent.as_posix());parent=parent.parent
            for directory in sorted(directories,key=lambda value:(value.count('/'),value)):
                files.mkdir(directory,0o700)
            for group,relative,record,stream in prepared:
                files.write('/'+group+'/'+relative.as_posix(),stream,
                    0o700 if record.get('executable') else 0o600)
            return {'files':len(prepared),'bytes':sum(record['size'] for _,_,record,_ in prepared)}
        finally:
            for *_,stream in prepared:stream.close()
