import hashlib
import io
import json
from pathlib import Path
import sqlite3
import tarfile
import tempfile
import unittest
from webapp.session_checkpoint import pack,unpack,persistent,BUNDLE,MANIFEST

class SessionCheckpointTests(unittest.TestCase):
    def test_runtime_skills_caches_and_locks_excluded_but_database_wal_retained(self):
        for name in ('codex/skills/.system/imagegen/SKILL.md','cache/file','codex/state_5.sqlite-shm','codex/thread-writer-locks/a.lock','codex/logs_2.sqlite','codex/logs_2.sqlite-wal','codex/config.toml'):
            self.assertFalse(persistent(name),name)
        for name in ('codex/state_5.sqlite','codex/state_5.sqlite-wal','codex/sessions/2026/session.jsonl','codex/installation_id'):
            self.assertTrue(persistent(name),name)
    def test_live_sqlite_main_and_wal_bundle_restore_committed_data(self):
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder);source=root/'state';(source/'codex').mkdir(parents=True)
            db=sqlite3.connect(source/'codex/state_5.sqlite');db.execute('PRAGMA journal_mode=WAL');db.execute('CREATE TABLE messages(body TEXT)');db.execute("INSERT INTO messages VALUES ('persisted')");db.commit()
            records={}
            for p in source.rglob('*'):
                if p.is_file() and persistent(str(p.relative_to(source))):records[str(p.relative_to(source))]={'size':p.stat().st_size,'sha256':hashlib.sha256(p.read_bytes()).hexdigest()}
            self.assertIn('codex/state_5.sqlite-wal',records)
            bundle=pack(source,records);restored=root/'restored';self.assertEqual(unpack(source/BUNDLE,restored,16*1024**2),records)
            with sqlite3.connect(restored/'codex/state_5.sqlite') as recovered:self.assertEqual(recovered.execute('SELECT body FROM messages').fetchone()[0],'persisted')
            db.close();self.assertEqual(len(bundle),1)
    def test_archive_traversal_symlinks_duplicates_and_size_limits_rejected(self):
        for kind in ('traversal','symlink','duplicate','size'):
            with self.subTest(kind=kind),tempfile.TemporaryDirectory() as folder:
                root=Path(folder);archive=root/'bad.tar.gz'
                with tarfile.open(archive,'w:gz') as tar:
                    name='../escape' if kind=='traversal' else 'codex/data'
                    item=tarfile.TarInfo(name);item.size=10
                    if kind=='symlink':item.type=tarfile.SYMTYPE;item.linkname='/tmp/escape';item.size=0
                    tar.addfile(item,io.BytesIO(b'a'*10))
                    if kind=='duplicate':tar.addfile(item,io.BytesIO(b'a'*10))
                with self.assertRaises(ValueError):unpack(archive,root/'dest',5 if kind=='size' else 1000)
                self.assertFalse((root/'escape').exists())
    def test_identical_session_data_has_stable_bundle_hash(self):
        with tempfile.TemporaryDirectory() as folder:
            hashes=[]
            for name in ('a','b'):
                root=Path(folder)/name;root.mkdir();(root/'conversation.json').write_bytes(b'unchanged')
                record={'conversation.json':{'size':9,'sha256':hashlib.sha256(b'unchanged').hexdigest()}}
                hashes.append(pack(root,record)[BUNDLE]['sha256'])
            self.assertEqual(hashes[0],hashes[1])

    def test_manifest_integrity_is_required(self):
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder);archive=root/'bad.tar.gz'
            with tarfile.open(archive,'w:gz') as tar:
                raw=b'{}';info=tarfile.TarInfo(MANIFEST);info.size=len(raw);tar.addfile(info,io.BytesIO(raw))
                info=tarfile.TarInfo('codex/data');info.size=1;tar.addfile(info,io.BytesIO(b'x'))
            with self.assertRaisesRegex(ValueError,'integrity'):unpack(archive,root/'dest',1024)

class DistributedSessionCheckpointTests(unittest.TestCase):
    def test_encrypted_session_bundle_restores_on_other_node_and_reuses_workspace(self):
        import base64,json,os,shutil
        from unittest.mock import patch
        from tests.distributed_support import PostgresServer,S3Server
        from webapp import agent_store,agent_sandbox
        from webapp.store import Store
        from webapp.task_leases import Coordinator
        from webapp.execution_runtime import TaskRuntime
        from webapp.execution_resources import IdentityPool
        from webapp.artifact_store import S3Objects
        from webapp.checkpoints import profile
        pg=PostgresServer();s3=S3Server()
        try:
            with tempfile.TemporaryDirectory(prefix='cs-') as folder,patch.dict(os.environ,{
                'VIDEO_USE_DATABASE_URL':pg.dsn,'VIDEO_USE_DISTRIBUTED':'1','VIDEO_USE_OBJECT_BUCKET':'session',
                'VIDEO_USE_OBJECT_ENDPOINT':s3.endpoint,'AWS_ACCESS_KEY_ID':'test','AWS_SECRET_ACCESS_KEY':'test',
                'AWS_DEFAULT_REGION':'us-east-1','VIDEO_USE_OBJECT_ENCRYPTION_KEY':base64.b64encode(b'z'*32).decode(),
                'VIDEO_USE_OBJECT_REQUIRE_ENCRYPTION':'1','VIDEO_USE_TASK_MEMORY_BYTES':str(256*1024**2),'VIDEO_USE_TASK_DISK_BYTES':str(64*1024**2)}):
                root=Path(folder);backend=S3Objects('session',s3.endpoint);backend.client.create_bucket(Bucket='session')
                stores=[Store(root/'a'),Store(root/'b')];agent_store.initialize(stores[0]);p=stores[0].add_project('owner','session')['id']
                first=None
                for index,s in enumerate([stores[0],stores[1],stores[0]]):
                    t=agent_store.add_turn(s,p,'plan','conversation');lease=Coordinator(s,'session-'+str(index),'test').claim(t,'agent');runtime=None;db=None
                    try:
                        with lease.bind():
                            s.execute("UPDATE agent_turns SET status='running' WHERE id=?",(t,))
                            runtime=TaskRuntime(lease,IdentityPool(root/('identity-'+str(index)),start=690000+100*index,size=4))
                            agent_sandbox.prepare(s,p,'test','plan',t)
                            db_path=runtime.state/'codex/state_5.sqlite'
                            object_count=s.query('SELECT COUNT(*) AS n FROM artifact_objects WHERE project=?',(p,))[0]['n']
                            if index==0:
                                static=runtime.state/'codex/skills/.system/test';static.mkdir(parents=True);(static/'SKILL.md').write_text('runtime-owned')
                                with sqlite3.connect(db_path) as db:
                                    db.execute('PRAGMA journal_mode=WAL');db.execute('CREATE TABLE conversation(message TEXT)');db.execute("INSERT INTO conversation VALUES ('remember this across nodes')");db.commit()
                                    s.execute('INSERT INTO execution_session_profiles VALUES (?,?)',(p,profile()))
                                    runtime.checkpoint()
                            else:
                                self.assertFalse((runtime.state/'codex/skills').exists())
                                with sqlite3.connect(db_path) as db:
                                    self.assertEqual(db.execute('SELECT message FROM conversation').fetchone()[0],'remember this across nodes')
                                    if index==2:
                                        db.execute("UPDATE conversation SET message='changed conversation'");db.commit()
                                    runtime.checkpoint()
                            manifest=json.loads(s.query('SELECT manifest FROM execution_checkpoints WHERE project=?',(p,))[0]['manifest'])
                            self.assertEqual(set(manifest['state']),{BUNDLE})
                            if index==0:first=manifest
                            else:
                                self.assertEqual({k:v['artifact'] for k,v in manifest['workspace'].items()},{k:v['artifact'] for k,v in first['workspace'].items()})
                                created=s.query('SELECT COUNT(*) AS n FROM artifact_objects WHERE project=?',(p,))[0]['n']-object_count
                                self.assertEqual(created,0 if index==1 else 1)
                                if index==1:self.assertEqual(manifest['state'][BUNDLE]['artifact'],first['state'][BUNDLE]['artifact'])
                                else:self.assertNotEqual(manifest['state'][BUNDLE]['artifact'],first['state'][BUNDLE]['artifact'])
                            agent_store.finish(s,t,'succeeded','reply')
                    finally:
                        if db:db.close()
                        if runtime:runtime.close()
                        lease.release()
        finally:s3.close();pg.close()
