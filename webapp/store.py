"""SQLite repository with short, fenced transactions and reentrant connections.

Multiple processes may share a LOCAL database. Multi-host deployments require a
server database adapter; never put SQLite WAL files on a network filesystem.
"""
from __future__ import annotations

import json
import sqlite3
import time
import uuid
import threading
import os
from contextlib import contextmanager
from pathlib import Path

try:
    from psycopg import IntegrityError as ServerIntegrityError
    INTEGRITY_ERRORS=(sqlite3.IntegrityError,ServerIntegrityError)
except ImportError:
    INTEGRITY_ERRORS=(sqlite3.IntegrityError,)


class Connection:
    def __init__(self, raw):
        self.raw = raw

    def execute(self, sql, args=()):
        # Existing operations explicitly request a write transaction. A lease
        # fence or an outer operation may already have started that transaction.
        if sql.strip().upper() == 'BEGIN IMMEDIATE' and self.raw.in_transaction:
            return self.raw.execute('SELECT 1')
        return self.raw.execute(sql, args)

    def executescript(self, sql):
        # These are trusted schema scripts only. sqlite3.executescript commits an
        # existing transaction implicitly, which would break migration/fence locks.
        statements=[s.strip() for s in sql.split(';') if s.strip()]
        for statement in statements:
            if statement.upper().startswith('PRAGMA JOURNAL_MODE') and not self.raw.in_transaction:
                self.raw.execute(statement)
        if not self.raw.in_transaction:self.raw.execute('BEGIN IMMEDIATE')
        for statement in statements:
            if not statement.upper().startswith('PRAGMA JOURNAL_MODE'):
                self.raw.execute(statement)


class Store:
    def __init__(self, root: Path,timeout=30,initialize_schema=True,dsn=None):
        self.root = root.resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        self.database = self.root / 'state.sqlite3'
        self.dsn = dsn if dsn is not None else os.environ.get('VIDEO_USE_DATABASE_URL', '')
        self.identity = self.dsn or str(self.database)
        if os.environ.get('VIDEO_USE_DISTRIBUTED') == '1' and not self.dsn:
            raise ValueError('Distributed execution requires VIDEO_USE_DATABASE_URL')
        if os.environ.get('VIDEO_USE_DISTRIBUTED')=='1' and not os.environ.get('VIDEO_USE_OBJECT_BUCKET'):
            raise ValueError('Distributed execution requires VIDEO_USE_OBJECT_BUCKET')
        if self.dsn and not self.dsn.startswith(('postgresql://', 'postgres://')):
            raise ValueError('VIDEO_USE_DATABASE_URL must select PostgreSQL')
        self.timeout=timeout
        self._connections = threading.local()
        if not initialize_schema:return
        with self.connect() as db:
            db.executescript('''
                PRAGMA journal_mode=WAL;
                CREATE TABLE IF NOT EXISTS projects (
                    id TEXT PRIMARY KEY, owner TEXT NOT NULL, name TEXT NOT NULL,
                    created REAL NOT NULL
                );
                CREATE TABLE IF NOT EXISTS assets (
                    id TEXT PRIMARY KEY, project TEXT NOT NULL, name TEXT NOT NULL,
                    filename TEXT NOT NULL, metadata TEXT NOT NULL, created REAL NOT NULL
                );
                CREATE TABLE IF NOT EXISTS jobs (
                    id TEXT PRIMARY KEY, project TEXT NOT NULL, kind TEXT NOT NULL,
                    status TEXT NOT NULL, payload TEXT NOT NULL, artifacts TEXT NOT NULL,
                    error TEXT NOT NULL, created REAL NOT NULL, updated REAL NOT NULL
                );
                CREATE INDEX IF NOT EXISTS projects_owner ON projects(owner);
                CREATE INDEX IF NOT EXISTS jobs_project ON jobs(project);
                CREATE TABLE IF NOT EXISTS project_drafts (
                    project TEXT PRIMARY KEY, preset TEXT, prompt TEXT NOT NULL,
                    editor TEXT NOT NULL, updated REAL NOT NULL
                );
                CREATE TABLE IF NOT EXISTS project_demo_context (
                    project TEXT PRIMARY KEY, demo TEXT NOT NULL
                );
            ''')

    @contextmanager
    def connect(self, readonly=False, *, snapshot=False):
        if snapshot and not readonly:raise ValueError('Observation snapshots must be read-only')
        existing = getattr(self._connections, 'db', None)
        if existing is not None:
            yield existing
            return
        if self.dsn:
            from .database import connect, PostgresConnection
            db = connect(self.dsn, self.timeout, readonly=readonly,snapshot=snapshot)
            connection = PostgresConnection(db)
        else:
            db = sqlite3.connect(self.database.as_uri()+'?mode=ro' if readonly else self.database,
                                 timeout=self.timeout,uri=readonly)
            db.row_factory = sqlite3.Row
            if readonly:db.execute('PRAGMA query_only=ON')
            connection = Connection(db)
        self._connections.db = connection
        notices=[];self._connections.notices=notices;committed=False
        try:
            with db:
                if snapshot and not self.dsn:connection.execute('BEGIN')
                from .task_leases import CURRENT
                lease = CURRENT.get()
                if lease is not None and lease.store.identity == self.identity:
                    if not readonly:connection.execute('BEGIN IMMEDIATE')
                    lease.validate(connection)
                yield connection
            committed=True
        finally:
            self._connections.db = None
            self._connections.notices = None
            db.close()
            if committed:
                for notice in notices:self._notify(notice)

    @staticmethod
    def _notify(callback):
        # Only best-effort notifications belong here, never business writes.
        try:callback()
        except Exception as exc:
            from .telemetry import emit
            emit('stream.transport_failed','WARNING',reason='after_commit',exception_type=type(exc).__name__)

    def notify_after_commit(self,callback):
        notices=getattr(self._connections,'notices',None)
        if notices is not None:notices.append(callback)
        else:self._notify(callback)

    def query(self, sql, args=()):
        with self.connect(readonly=True) as db:
            return [dict(row) for row in db.execute(sql, args).fetchall()]

    def execute(self, sql, args=()):
        with self.connect() as db:
            return db.execute(sql, args).rowcount

    def now(self):
        from .task_leases import now
        with self.connect() as db:return now(db)

    def project_dir(self, project):
        # Identifiers come only from database records, never user paths.
        return self.root / 'projects' / project

    @property
    def shared_files(self):
        return bool(os.environ.get('VIDEO_USE_OBJECT_BUCKET'))

    @property
    def artifacts(self):
        if not hasattr(self, '_artifact_store'):
            from .artifact_store import ArtifactStore
            self._artifact_store = ArtifactStore(self)
        return self._artifact_store

    def file(self, project, relative):
        from .artifact_store import logical_path
        logical_path(relative)
        return self.artifacts.load_file(project,relative) if self.shared_files else self.project_dir(project)/relative

    def has_file(self,project,relative):
        from .artifact_store import logical_path
        logical_path(relative)
        if self.shared_files:
            return bool(self.query("SELECT f.path FROM artifact_files f JOIN artifact_objects o ON o.id=f.artifact AND o.project=f.project WHERE f.project=? AND f.path=? AND o.state IN ('staged','published')",(project,relative)))
        path=self.project_dir(project)/relative
        return path.is_file() and not path.is_symlink()

    def restore_files(self, project, prefix):
        if not self.shared_files:return
        from .artifact_store import logical_path
        logical_path(prefix)
        self.artifacts
        for row in self.query('SELECT path FROM artifact_files WHERE project=? AND path LIKE ?', (project,prefix+'/%')):
            self.artifacts.load_file(project,row['path'])

    def add_project(self, owner, name):
        project = {'id': uuid.uuid4().hex, 'owner': owner, 'name': name, 'created': time.time()}
        self.execute('INSERT INTO projects VALUES (:id, :owner, :name, :created)', project)
        (self.project_dir(project['id']) / 'sources').mkdir(parents=True)
        (self.project_dir(project['id']) / 'edit' / 'transcripts').mkdir(parents=True)
        return project

    def add_asset(self, project, asset_id, name, filename, metadata, artifact=None):
        if self.shared_files and artifact is None:
            artifact=self.artifacts.upload(project,self.project_dir(project)/'sources'/filename)
        with self.connect():
            self.execute('INSERT INTO assets VALUES (?, ?, ?, ?, ?, ?)',
                         (asset_id, project, name, filename, json.dumps(metadata), time.time()))
            if artifact:self.artifacts.publish(project,'sources/'+filename,artifact['id'],expected_revision=0)

    def draft(self, project):
        rows = self.query('SELECT preset,prompt,editor FROM project_drafts WHERE project=?', (project,))
        if not rows:
            return {'preset':None, 'prompt':'', 'editor':'chat', 'demo':self.demo_context(project)}
        row=rows[0]
        row['preset']=json.loads(row['preset']) if row['preset'] else None
        row['demo']=self.demo_context(project)
        return row

    def demo_context(self, project):
        rows=self.query('SELECT demo FROM project_demo_context WHERE project=?',(project,))
        return rows[0]['demo'] if rows else None

    def set_demo_context(self, project, demo):
        self.execute('INSERT INTO project_demo_context VALUES (?,?) ON CONFLICT(project) DO UPDATE SET demo=excluded.demo',(project,demo))

    def save_draft(self, project, preset, prompt, editor):
        self.execute('INSERT INTO project_drafts VALUES (?,?,?,?,?) ON CONFLICT(project) DO UPDATE SET preset=excluded.preset,prompt=excluded.prompt,editor=excluded.editor,updated=excluded.updated',
                     (project,json.dumps(preset) if preset is not None else None,prompt,editor,time.time()))

    def assets(self, project):
        rows = self.query('SELECT * FROM assets WHERE project=? ORDER BY created', (project,))
        for row in rows:
            row['metadata'] = json.loads(row['metadata'])
        return rows

    def admit_tasks(self, project, count=1,*,include_open_runs=False):
        """Call inside the submission transaction; limits span all queue types."""
        owner=self.query('SELECT owner FROM projects WHERE id=?',(project,))[0]['owner']
        rows=self.query("""SELECT p.owner FROM projects p JOIN execution_subjects t ON t.project=p.id
            WHERE t.status IN ('queued','running','waiting')
              AND NOT EXISTS(SELECT 1 FROM runs r WHERE r.id=t.id AND r.status='WAITING_USER')""")
        if len(rows)+count>int(os.environ.get('VIDEO_USE_QUEUE_LIMIT','100')):
            raise ValueError('任务队列已满，请稍后重试。')
        if sum(r['owner']==owner for r in rows)+count>int(os.environ.get('VIDEO_USE_USER_QUEUE_LIMIT','4')):
            raise ValueError('你的待处理任务已达上限，请等待已有任务完成。')
        if include_open_runs:
            open_runs=self.query("""SELECT p.owner FROM projects p JOIN runs r ON r.project_id=p.id
                WHERE r.status NOT IN ('SUCCEEDED','FAILED','CANCELLED')""")
            if sum(r['owner']==owner for r in open_runs)+count>int(os.environ.get('VIDEO_USE_USER_OPEN_RUN_LIMIT','50')):
                raise ValueError('你的待确认任务过多，请先处理或取消已有任务。')

    def check_storage(self,project,extra=0,replace_checkpoint=False,replace_turn=None,replace_step=None):
        """Application retention quota; caller commits metadata in this transaction."""
        owner=self.query('SELECT owner FROM projects WHERE id=?',(project,))[0]['owner']
        total=sum(json.loads(r['metadata']).get('size',0) for r in self.query(
            'SELECT a.metadata FROM assets a JOIN projects p ON p.id=a.project WHERE p.owner=?',(owner,)))
        for row in self.query('SELECT t.id,t.artifacts FROM agent_turns t JOIN projects p ON p.id=t.project WHERE p.owner=?',(owner,)):
            if row['id']!=replace_turn:total+=sum(a.get('size',0) for a in json.loads(row['artifacts']))
        checkpoint_query='''SELECT c.project,c.manifest FROM execution_checkpoint_versions c JOIN projects p ON p.id=c.project WHERE p.owner=?
          UNION ALL SELECT c.project,c.manifest FROM execution_checkpoints c JOIN projects p ON p.id=c.project
          WHERE p.owner=? AND NOT EXISTS(SELECT 1 FROM execution_checkpoint_versions v WHERE v.path=c.path)'''
        for row in self.query(checkpoint_query,(owner,owner)):
            if replace_checkpoint and row['project']==project:continue
            total+=sum(item['size'] for group in json.loads(row['manifest']).values() for item in group.values())
        for row in self.query('SELECT c.workflow,c.step,c.revision,c.manifest FROM execution_step_checkpoints c JOIN projects p ON p.id=c.project WHERE p.owner=?',(owner,)):
            if replace_step==(row['workflow'],row['step'],row['revision']):continue
            total+=sum(item['size'] for item in json.loads(row['manifest']).values())
        total+=self.query('SELECT COALESCE(SUM(r.bytes),0) AS n FROM execution_job_results r JOIN jobs j ON j.id=r.job_id JOIN projects p ON p.id=j.project WHERE p.owner=?',(owner,))[0]['n']
        if total+extra>int(os.environ.get('VIDEO_USE_USER_STORAGE_BYTES',str(20*1024**3))):
            raise ValueError('你的存储额度不足，请清理旧项目或增加额度。')

    def add_job(self, project, kind, payload):
        now = self.now()
        job_id = uuid.uuid4().hex
        with self.connect() as db:
            db.execute('INSERT INTO jobs VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)',
                     (job_id, project, kind, 'queued', json.dumps(payload), '[]', '', now, now))
            if db.execute("SELECT name FROM sqlite_master WHERE name='observation_tasks'").fetchone():
                from .observation_schema import register
                register(db,job_id,project,now)
            if db.execute("SELECT name FROM sqlite_master WHERE name='usage_tasks'").fetchone():
                from .task_usage import attach
                attach(db,job_id,project,now)
        if self.query("SELECT name FROM sqlite_master WHERE name='execution_limits'"):
            from .task_lifecycle import ensure
            with self.connect() as db:ensure(db,job_id,now)
        return self.job(job_id)

    def job(self, job_id):
        rows = self.query('SELECT * FROM jobs WHERE id=?', (job_id,))
        if not rows:
            return None
        row = rows[0]
        row['payload'] = json.loads(row['payload'])
        row['artifacts'] = json.loads(row['artifacts'])
        return row

    def finish(self, job_id, status, artifacts=None, error=''):
        self.execute('UPDATE jobs SET status=?, artifacts=?, error=?, updated=? WHERE id=?',
                     (status, json.dumps(artifacts or []), error, time.time(), job_id))
