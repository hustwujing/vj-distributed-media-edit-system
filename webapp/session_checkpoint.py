"""Portable session data, excluding reproducible runtime files and volatile locks."""
import hashlib
import gzip
import io
import json
import os
from pathlib import Path,PurePosixPath
import tarfile

BUNDLE='session.tar.gz'
FORMAT='session-bundle-v1'
MANIFEST='__session_manifest__.json'


def persistent(path):
    parts=PurePosixPath(path).parts
    if not parts or parts[0] in ('cache','tmp','.cache'):return False
    if parts[0]=='codex':
        if len(parts)>1 and parts[1] in ('skills','shell_snapshots','thread-writer-locks','log','logs','tmp','cache'):return False
        if parts[-1] in ('config.toml','auth.json','.sandbox_migration'):return False
        if parts[-1].startswith('logs_') or parts[-1].endswith(('-shm','.lock','.log')):return False
    if parts[0] in ('claude','dsh'):
        if any(part in ('cache','.cache','tmp','logs','debug','plugins','skills','profiles') for part in parts[1:]):return False
        if parts[-1] in ('config.json','settings.json','settings.local.json','auth.json','.credentials.json'):return False
        if parts[-1].endswith(('.lock','.log','-shm')):return False
    return True


def pack(folder,records):
    folder=Path(folder)
    if MANIFEST in records or BUNDLE in records:raise ValueError('Reserved session filename')
    path=folder/BUNDLE
    encoded=json.dumps(records,sort_keys=True,separators=(',',':')).encode()
    with path.open('wb') as raw, gzip.GzipFile(filename='',mode='wb',fileobj=raw,mtime=0) as compressed, tarfile.open(fileobj=compressed,mode='w',format=tarfile.PAX_FORMAT) as archive:
        info=tarfile.TarInfo(MANIFEST);info.size=len(encoded);info.mode=0o600;archive.addfile(info,io.BytesIO(encoded))
        for name,record in sorted(records.items()):
            info=tarfile.TarInfo(name);info.size=record['size'];info.mode=0o600
            with (folder/name).open('rb') as source:archive.addfile(info,source)
    path.chmod(0o600)
    with path.open('rb') as stream:record={'size':path.stat().st_size,'sha256':hashlib.file_digest(stream,'sha256').hexdigest(),'format':FORMAT}
    # Only the bundle is a checkpoint object. Plain frozen files are no longer
    # needed after packaging; live source state belongs to another directory.
    for name in records:(folder/name).unlink()
    return {BUNDLE:record}


def unpack(path,destination,limit):
    destination=Path(destination);destination.mkdir(mode=0o700,parents=True,exist_ok=False)
    records=None;seen=set();actual={};size=0
    with tarfile.open(path,'r:gz') as archive:
        for index,member in enumerate(archive):
            if index>20000 or not member.isfile():raise ValueError('Invalid session archive member')
            name=member.name;parts=PurePosixPath(name).parts
            if not parts or name.startswith('/') or any(p in ('..','.') for p in parts) or str(PurePosixPath(name))!=name or '\\' in name or name in seen:raise ValueError('Unsafe session path')
            seen.add(name)
            if member.size<0 or size+member.size>limit:raise ValueError('Session expansion exceeds limit')
            size+=member.size;source=archive.extractfile(member)
            if name==MANIFEST:
                if member.size>4*1024**2:raise ValueError('Session manifest too large')
                records=json.loads(source.read());continue
            if not persistent(name):raise ValueError('Nonpersistent session entry')
            target=destination/name;target.parent.mkdir(mode=0o700,parents=True,exist_ok=True)
            digest=hashlib.sha256();copied=0
            with target.open('xb') as output:
                while chunk:=source.read(1024*1024):
                    copied+=len(chunk);digest.update(chunk);output.write(chunk)
                output.flush();os.fsync(output.fileno())
            target.chmod(0o600)
            if copied!=member.size:raise ValueError('Truncated session file')
            actual[name]={'size':copied,'sha256':digest.hexdigest()}
    if not isinstance(records,dict) or records!=actual:raise ValueError('Session integrity mismatch')
    return actual
