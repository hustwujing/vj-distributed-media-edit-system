"""Immutable, project-owned objects and fenced logical-file publication.

Object upload precedes reference publication. A staged object is never a
deliverable. Node-local directories are disposable verified caches.
"""
from contextlib import contextmanager
import hashlib
import os
from pathlib import Path, PurePosixPath
import shutil
import stat
import tempfile
import uuid
from .execution_metrics import measured


def logical_path(value):
    path = PurePosixPath(value)
    if not value or path.is_absolute() or '..' in path.parts or '\\' in value or str(path)!=value:
        raise ValueError('Invalid logical artifact path')
    return value


class LocalObjects:
    """Single-node development backend, not a cross-host storage transport."""
    def __init__(self, root):self.root = Path(root)
    def put(self, key, source):
        target = self.root/logical_path(key)
        target.parent.mkdir(parents=True,exist_ok=True)
        with target.open('xb') as output:shutil.copyfileobj(source,output)
    @contextmanager
    def get(self,key,max_bytes=None):
        with (self.root/logical_path(key)).open('rb') as stream:yield stream
    def delete(self,key):(self.root/logical_path(key)).unlink(missing_ok=True)


class S3Objects:
    def __init__(self, bucket, endpoint=None, **credentials):
        import boto3
        from botocore.config import Config
        self.bucket = bucket
        self.session=boto3.Session(**credentials)
        self.endpoint=endpoint
        self.client = self.session.client('s3',endpoint_url=endpoint or None,
            config=Config(signature_version='s3v4',connect_timeout=5,read_timeout=30,retries={'max_attempts':2},s3={'addressing_style':'path'},
                          request_checksum_calculation='when_required',response_checksum_validation='when_required'),
            )
        from .object_encryption import configured_key
        self.encryption_key=configured_key()
        if os.environ.get('VIDEO_USE_OBJECT_REQUIRE_ENCRYPTION')=='1' and not self.encryption_key:
            raise ValueError('This object backend requires client-side encryption')
    def transfer(self,action,key,stream,max_bytes=None):
        """Non-interruptible SDK calls run in an owned, cancellable process."""
        import json,subprocess,sys
        from .task_leases import CURRENT
        from .cancellation import for_store,stop_process
        lease=CURRENT.get()
        credentials=self.session.get_credentials().get_frozen_credentials()
        request={'action':action,'bucket':self.bucket,'endpoint':self.endpoint,'key':logical_path(key),
                 'fd':stream.fileno(),'parent':os.getpid(),'max_bytes':max_bytes,
                 'credentials':{'aws_access_key_id':credentials.access_key,'aws_secret_access_key':credentials.secret_key,
                                'aws_session_token':credentials.token,'region_name':self.session.region_name or 'us-east-1'}}
        call=for_store(lease.store).register_lease(lease,'object_'+action) if lease else None
        process=None
        try:
            if call:call.check()
            process=subprocess.Popen([sys.executable,'-m','webapp.artifact_transfer'],stdin=subprocess.PIPE,
                stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,pass_fds=(stream.fileno(),),start_new_session=True)
            if call:call.install(lambda:stop_process(process))
            process.communicate(json.dumps(request).encode(),timeout=300)
            if call:call.check()
            if process.returncode:raise OSError('Object transfer failed; no artifact was published')
        finally:
            if process and process.poll() is None:stop_process(process)
            if call:call.close()
    def put(self,key,source):
        if self.encryption_key:
            from .object_encryption import encrypt
            from .task_leases import CURRENT
            from .cancellation import process_call
            with tempfile.TemporaryFile() as encrypted,process_call(CURRENT.get(),'object_encrypt') as call:
                encrypt(source,encrypted,self.encryption_key,key,call.check if call else lambda:None)
                return self._put(key,encrypted)
        return self._put(key,source)

    def _put(self,key,source):
        # UUID object names and conditional writes prevent accidental replacement.
        from .task_leases import CURRENT
        if CURRENT.get() is not None:return self.transfer('put',key,source)
        self.client.put_object(Bucket=self.bucket,Key=logical_path(key),Body=source,IfNoneMatch='*')
    @contextmanager
    def get(self,key,max_bytes=None):
        if self.encryption_key:
            from .object_encryption import decrypt,OVERHEAD
            from .task_leases import CURRENT
            from .cancellation import process_call
            limit=max_bytes if max_bytes is not None else 8*1024**3
            with self._get(key,limit+OVERHEAD) as encrypted,tempfile.TemporaryFile() as plaintext,process_call(CURRENT.get(),'object_decrypt') as call:
                decrypt(encrypted,plaintext,self.encryption_key,key,limit,call.check if call else lambda:None)
                yield plaintext
            return
        with self._get(key,max_bytes) as source:yield source

    @contextmanager
    def _get(self,key,max_bytes=None):
        from .task_leases import CURRENT
        if CURRENT.get() is not None:
            with tempfile.TemporaryFile() as stream:
                self.transfer('get',key,stream,max_bytes)
                stream.seek(0)
                yield stream
            return
        response=self.client.get_object(Bucket=self.bucket,Key=logical_path(key))
        try:yield response['Body']
        finally:response['Body'].close()
    def delete(self,key):self.client.delete_object(Bucket=self.bucket,Key=logical_path(key))


class MetadataOnlyObjects:
    """Control-plane references require no object credentials or byte access."""
    def __getattr__(self,name):
        raise PermissionError('Object byte access is disabled for this process')


class ArtifactStore:
    def __init__(self,store,backend=None):
        self.store=store
        bucket=os.environ.get('VIDEO_USE_OBJECT_BUCKET')
        if backend is None and os.environ.get('VIDEO_USE_DISTRIBUTED')=='1' and not bucket:
            raise ValueError('Distributed execution requires VIDEO_USE_OBJECT_BUCKET')
        self.backend=backend or (MetadataOnlyObjects() if os.environ.get('VIDEO_USE_STORAGE_METADATA_ONLY')=='1' else
                                S3Objects(bucket,os.environ.get('VIDEO_USE_OBJECT_ENDPOINT')) if bucket else LocalObjects(store.root/'objects'))
        if isinstance(self.backend,MetadataOnlyObjects):return
        with store.connect() as db:
            db.executescript('''CREATE TABLE IF NOT EXISTS artifact_objects(
              id TEXT PRIMARY KEY,project TEXT NOT NULL,object_key TEXT NOT NULL UNIQUE,
              sha256 TEXT NOT NULL,size BIGINT NOT NULL,state TEXT NOT NULL,created REAL NOT NULL,expires REAL NOT NULL);
              CREATE TABLE IF NOT EXISTS artifact_files(
              project TEXT NOT NULL,path TEXT NOT NULL,artifact TEXT NOT NULL,revision INTEGER NOT NULL,
              PRIMARY KEY(project,path));
              CREATE INDEX IF NOT EXISTS artifact_object_project ON artifact_objects(project,state);''')

    @measured('output_upload')
    def upload(self,project,path,*,check=None):
        if isinstance(self.backend,MetadataOnlyObjects):raise PermissionError('Object byte access is disabled for this process')
        check=check or (lambda:None)
        check()
        if not self.store.query('SELECT id FROM projects WHERE id=?',(project,)):raise ValueError('Unknown artifact project')
        path=Path(path)
        fd=os.open(path,os.O_RDONLY|os.O_NOFOLLOW|os.O_NONBLOCK)
        identifier=uuid.uuid4().hex
        prefix=os.environ.get('VIDEO_USE_OBJECT_PREFIX','video-use').strip('/')
        if prefix:logical_path(prefix)
        key=(prefix+'/' if prefix else '')+f'projects/{project}/objects/{identifier}'
        with os.fdopen(fd,'rb') as source,tempfile.TemporaryFile() as frozen:
            info=os.fstat(source.fileno())
            if not stat.S_ISREG(info.st_mode) or info.st_nlink!=1:raise ValueError('Artifact must be a regular private file')
            digest=hashlib.sha256();size=0
            limit=int(os.environ.get('VIDEO_USE_ARTIFACT_MAX_BYTES',str(8*1024**3)))
            while chunk:=source.read(1024*1024):
                check()
                size+=len(chunk)
                if size>limit:raise ValueError('Artifact exceeds size limit')
                digest.update(chunk);frozen.write(chunk)
            if size!=info.st_size:raise ValueError('Artifact changed during upload preparation')
            now=self.store.now()
            with self.store.connect() as db:
                db.execute('BEGIN IMMEDIATE')
                owner=db.execute('SELECT owner FROM projects WHERE id=?',(project,)).fetchone()
                if not owner:raise ValueError('Artifact project was deleted')
                pending=db.execute("SELECT COALESCE(SUM(o.size),0) AS bytes FROM artifact_objects o JOIN projects p ON p.id=o.project WHERE p.owner=? AND o.state IN ('uploading','staged')",(owner['owner'],)).fetchone()['bytes']
                self.store.check_storage(project,size+pending)
                db.execute('INSERT INTO artifact_objects VALUES (?,?,?,?,?,?,?,?)',
                    (identifier,project,key,digest.hexdigest(),size,'uploading',now,now+3600))
            frozen.seek(0)
            check()
            self.backend.put(key,frozen)
            check()
        if not self.store.execute("UPDATE artifact_objects SET state='staged' WHERE id=? AND state='uploading'",(identifier,)):
            raise RuntimeError('Upload reservation expired')
        return {'id':identifier,'sha256':digest.hexdigest(),'size':size}

    def record(self,project,identifier):
        rows=self.store.query("SELECT * FROM artifact_objects WHERE id=? AND project=? AND state IN ('staged','published')",(identifier,project))
        if not rows:raise ValueError('Artifact missing, unavailable or outside project')
        return rows[0]

    def publish(self,project,path,identifier,expected_revision=None):
        logical_path(path)
        with self.store.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            record=self.record(project,identifier)
            old=db.execute('SELECT revision,artifact FROM artifact_files WHERE project=? AND path=?',(project,path)).fetchone()
            revision=old['revision'] if old else 0
            if expected_revision is not None and expected_revision!=revision:raise ValueError('Artifact revision changed')
            db.execute('INSERT INTO artifact_files VALUES (?,?,?,?) ON CONFLICT(project,path) DO UPDATE SET artifact=excluded.artifact,revision=excluded.revision',
                       (project,path,identifier,revision+1))
            db.execute("UPDATE artifact_objects SET state='published' WHERE id=?",(record['id'],))
            if old and old['artifact']!=identifier:
                db.execute("UPDATE artifact_objects SET state='staged',expires=? WHERE id=? AND NOT EXISTS (SELECT 1 FROM artifact_files WHERE artifact=?)",
                           (self.store.now()+3600,old['artifact'],old['artifact']))
        return revision+1

    def resolve(self,project,path):
        logical_path(path)
        rows=self.store.query('SELECT artifact,revision FROM artifact_files WHERE project=? AND path=?',(project,path))
        if not rows:raise FileNotFoundError(path)
        return {**self.record(project,rows[0]['artifact']),'revision':rows[0]['revision']}

    @contextmanager
    def open(self,project,identifier):
        # Only expose verified bytes; never feed unverified remote content to tools.
        cached=self.materialize(project,identifier)
        with cached.open('rb') as stream:yield stream

    @measured('input_download')
    def materialize(self,project,identifier,target=None):
        if isinstance(self.backend,MetadataOnlyObjects):raise PermissionError('Object byte access is disabled for this process')
        record=self.record(project,identifier)
        destination=Path(target) if target else self.store.root/'cache/objects'/project/record['sha256']
        destination.parent.mkdir(parents=True,exist_ok=True)
        if any(p.is_symlink() for p in (destination,*destination.parents)):
            raise ValueError('Artifact cache path contains a symlink')
        if destination.is_file() and destination.stat().st_size==record['size']:
            with destination.open('rb') as source:
                if hashlib.file_digest(source,'sha256').hexdigest()==record['sha256']:return destination
        fd,temporary=tempfile.mkstemp(prefix='.artifact-',dir=destination.parent)
        try:
            digest=hashlib.sha256();size=0
            with os.fdopen(fd,'wb') as output,self.backend.get(record['object_key'],max_bytes=record['size']) as source:
                while chunk:=source.read(1024*1024):
                    size+=len(chunk)
                    if size>record['size']:raise ValueError('Artifact size mismatch')
                    digest.update(chunk);output.write(chunk)
                output.flush();os.fsync(output.fileno())
            if size!=record['size'] or digest.hexdigest()!=record['sha256']:raise ValueError('Artifact integrity mismatch')
            os.replace(temporary,destination)
        finally:
            Path(temporary).unlink(missing_ok=True)
        return destination

    def save_file(self,project,path):
        logical_path(path)
        value=self.upload(project,self.store.project_dir(project)/path)
        self.publish(project,path,value['id'])
        return value

    def load_file(self,project,path):
        record=self.resolve(project,path)
        return self.materialize(project,record['id'],self.store.project_dir(project)/path)

    def release_prefix(self,project,prefix=None):
        """Remove logical references transactionally; defer remote deletion."""
        if prefix is not None:logical_path(prefix)
        with self.store.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            predicate='project=?'+(' AND path LIKE ?' if prefix is not None else '')
            args=(project,prefix+'/%') if prefix is not None else (project,)
            db.execute('DELETE FROM artifact_files WHERE '+predicate,args)
            db.execute("""UPDATE artifact_objects SET state='staged',expires=?
              WHERE project=? AND state='published' AND NOT EXISTS
              (SELECT 1 FROM artifact_files f WHERE f.artifact=artifact_objects.id)""",
              (self.store.now()+3600,project))

    def collect_garbage(self,limit=100,project=None):
        if isinstance(self.backend,MetadataOnlyObjects):raise PermissionError('Object byte access is disabled for this process')
        # Claim deletion before network I/O. Publication rejects deleting objects.
        with self.store.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            scope=' AND project=?' if project is not None else ''
            arguments=(self.store.now(),project,limit) if project is not None else (self.store.now(),limit)
            rows=db.execute("SELECT id,object_key FROM artifact_objects o WHERE state IN ('uploading','staged','deleting','deleted') AND expires<?"+scope+" AND NOT EXISTS (SELECT 1 FROM artifact_files f WHERE f.artifact=o.id) ORDER BY CASE WHEN state='deleted' THEN 1 ELSE 0 END,expires LIMIT ?",arguments).fetchall()
            for row in rows:db.execute("UPDATE artifact_objects SET state='deleting' WHERE id=?",(row['id'],))
        for row in rows:
            self.backend.delete(row['object_key'])
            # Keep a tombstone: a timed-out PUT may finish after the first DELETE.
            # Reconciliation will delete that same immutable key again, never a
            # newer artifact or a published reference.
            self.store.execute("UPDATE artifact_objects SET state='deleted',expires=? WHERE id=? AND state='deleting'",(self.store.now()+86400,row['id']))
        return len(rows)
