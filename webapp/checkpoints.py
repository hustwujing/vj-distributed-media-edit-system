"""Retained project state versions and explicit, ownership-checked rollback."""
import hashlib
import json
from pathlib import Path
import uuid


def profile():
    skill=Path(__file__).resolve().parents[1]/'runtime/public/SKILL.md'
    from .agent_profiles import current
    from .runtime_image import fingerprint
    identity=current()
    value=skill.read_bytes()+identity.digest.encode()+fingerprint().encode()
    return 'agent-session-v2-'+hashlib.sha256(value).hexdigest()


def initialize(db):
    db.executescript('''
      CREATE TABLE IF NOT EXISTS execution_checkpoint_versions(
        id TEXT PRIMARY KEY,project TEXT NOT NULL,attempt TEXT NOT NULL,path TEXT NOT NULL UNIQUE,
        manifest TEXT NOT NULL,changes TEXT NOT NULL,profile TEXT NOT NULL,
        thread_id TEXT,created REAL NOT NULL
      );
      CREATE TABLE IF NOT EXISTS execution_session_profiles(project TEXT PRIMARY KEY,profile TEXT NOT NULL);
      CREATE TABLE IF NOT EXISTS execution_native_descriptors(
        path TEXT PRIMARY KEY,project TEXT NOT NULL,descriptor TEXT NOT NULL);
    ''')


def native_descriptor(store, project, path):
    rows=store.query('SELECT descriptor FROM execution_native_descriptors WHERE project=? AND path=?',(project,path))
    return json.loads(rows[0]['descriptor']) if rows else None


def save_native_descriptor(store, db, project, path, manifest, runtime_profile):
    """Commit an outer envelope with the checkpoint; native files stay opaque."""
    if not db.execute("SELECT name FROM sqlite_master WHERE name='agent_native_sessions'").fetchone():return
    rows=store.query('SELECT * FROM agent_native_sessions WHERE project=?',(project,))
    if not rows:return
    from .agent_profiles import current
    from .task_leases import CURRENT
    lease=CURRENT.get();identity=current();native=rows[0]
    # A task with a fresh configuration must never label another harness's
    # surviving database row as its own newly committed native state.
    if native['configuration_digest']!=identity.digest:return
    cursor=db.execute('SELECT MAX(seq) n FROM agent_context_events WHERE task_id=?',
                      (lease.run_id if lease else '',)).fetchone()['n'] or 0
    value={'schema':'opaque-session-envelope-v1','native_format':'sdk_owned_opaque',
           'profile':runtime_profile,'native':native,'tool_policy':identity.tool_policy,
           'task_id':lease.run_id if lease else None,'event_cursor':cursor,
           'storage':{'checkpoint':path,'state':manifest['state']}}
    db.execute('INSERT INTO execution_native_descriptors VALUES (?,?,?)',
               (path,project,json.dumps(value,sort_keys=True)))


def state_compatible(store, project, path):
    descriptor=native_descriptor(store,project,path)
    if descriptor:
        return descriptor.get('schema')=='opaque-session-envelope-v1' and descriptor.get('profile')==profile()
    rows=store.query('SELECT profile FROM execution_session_profiles WHERE project=?',(project,))
    return bool(rows and rows[0]['profile']==profile())


def committed_session(store, project):
    """A new uncheckpointed session ID cannot replace the committed session."""
    checkpoints=store.query('SELECT path FROM execution_checkpoints WHERE project=?',(project,))
    if not checkpoints:return None
    path=checkpoints[0]['path']
    if not state_compatible(store,project,path):return None
    descriptor=native_descriptor(store,project,path)
    if descriptor:return descriptor['native']['native_session_id']
    rows=store.query('SELECT thread_id FROM agent_sessions WHERE project=?',(project,))
    return rows[0]['thread_id'] if rows else None


def versions(store,project):
    rows=store.query('SELECT id,attempt,changes,created,path FROM execution_checkpoint_versions WHERE project=? ORDER BY created DESC',(project,))
    current=store.query('SELECT path FROM execution_checkpoints WHERE project=?',(project,))
    return [{'id':r['id'],'created':r['created'],'changes':json.loads(r['changes']),
             'current':bool(current and r['path']==current[0]['path'])} for r in rows]


def rollback(store,project,version):
    with store.connect() as db:
        db.execute('BEGIN IMMEDIATE')
        busy=db.execute("SELECT id FROM agent_turns WHERE project=? AND status IN ('queued','running','waiting','paused','cancelling') UNION ALL SELECT id FROM jobs WHERE project=? AND status IN ('queued','running')",(project,project)).fetchone()
        if busy:raise ValueError('请先停止当前任务，再恢复项目版本')
        resources=db.execute('''SELECT l.run_id FROM execution_leases l
          JOIN (SELECT id,project FROM execution_subjects) t ON t.id=l.run_id
          WHERE t.project=? AND (l.active=1 OR EXISTS(
            SELECT 1 FROM execution_cleanup c WHERE c.task_id=t.id AND c.state<>'complete')) LIMIT 1''',(project,)).fetchone()
        if resources:raise ValueError('请等待执行资源回收完成，再恢复项目版本')
        row=db.execute('SELECT * FROM execution_checkpoint_versions WHERE id=? AND project=?',(version,project)).fetchone()
        if not row:raise ValueError('项目版本不存在或已超过保留期限')
        if not store.shared_files and not (store.root/row['path']).is_dir():raise ValueError('版本文件不可用')
        db.execute('INSERT INTO execution_checkpoints VALUES (?,?,?,?,?) ON CONFLICT(project) DO UPDATE SET attempt=excluded.attempt,path=excluded.path,manifest=excluded.manifest,updated=excluded.updated',
                   (project,row['attempt'],row['path'],row['manifest'],store.now()))
        if row['profile']==profile() and row['thread_id']:
            db.execute('INSERT INTO agent_sessions VALUES (?,?) ON CONFLICT(project) DO UPDATE SET thread_id=excluded.thread_id',(project,row['thread_id']))
        else:
            db.execute('DELETE FROM agent_sessions WHERE project=?',(project,))
        db.execute('INSERT INTO execution_session_profiles VALUES (?,?) ON CONFLICT(project) DO UPDATE SET profile=excluded.profile',(project,row['profile']))
        # Never retain the descriptor of a newer, now rolled-back session.
        descriptor=native_descriptor(store,project,row['path'])
        db.execute('DELETE FROM agent_native_sessions WHERE project=?',(project,))
        if descriptor and descriptor.get('profile')==profile():
            native=descriptor['native']
            db.execute('INSERT INTO agent_native_sessions VALUES (?,?,?,?,?,?,?,?)',
                       tuple(native[k] for k in ('project','harness','adapter_version','runtime_profile','configuration_digest','native_session_id','restore_mode','updated')))
    return versions(store,project)
