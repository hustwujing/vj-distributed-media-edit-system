import io
from pathlib import Path
import shutil
import tempfile
import unittest
import uuid

from tests.distributed_support import PostgresServer,S3Server
from webapp import agent_store
from webapp.artifact_store import ArtifactStore,S3Objects
from webapp.store import Store
from webapp.task_leases import Coordinator,LeaseLost


class DistributedArtifactTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.pg=PostgresServer();cls.s3=S3Server()
        cls.backend=S3Objects('vu-distributed-tests',cls.s3.endpoint,
                             aws_access_key_id='test',aws_secret_access_key='test',region_name='us-east-1')
        cls.backend.client.create_bucket(Bucket=cls.backend.bucket)

    @classmethod
    def tearDownClass(cls):cls.s3.close();cls.pg.close()

    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();root=Path(self.temp.name)
        self.a=Store(root/'a',dsn=self.pg.dsn);agent_store.initialize(self.a)
        self.b=Store(root/'b',dsn=self.pg.dsn)
        self.files_a=ArtifactStore(self.a,self.backend);self.files_b=ArtifactStore(self.b,self.backend)
        self.project=self.a.add_project(self.temp.name,'objects')['id']
        self.path=root/'source.bin';self.path.write_bytes(b'verified cross-node data'*1000)

    def tearDown(self):self.temp.cleanup()

    def test_other_node_reads_after_original_directory_disappears(self):
        uploaded=self.files_a.upload(self.project,self.path)
        self.files_a.publish(self.project,'edit/output.bin',uploaded['id'],expected_revision=0)
        shutil.rmtree(self.a.root)
        record=self.files_b.resolve(self.project,'edit/output.bin')
        with self.files_b.open(self.project,record['id']) as stream:self.assertEqual(stream.read(),self.path.read_bytes())

    def test_upload_is_not_published_and_stale_owner_cannot_publish(self):
        task=agent_store.add_turn(self.a,self.project,'plan','artifact')
        ca=Coordinator(self.a,uuid.uuid4().hex,'test');cb=Coordinator(self.b,uuid.uuid4().hex,'test')
        first=ca.claim(task,'agent')
        with first.bind():uploaded=self.files_a.upload(self.project,self.path)
        with self.assertRaises(FileNotFoundError):self.files_b.resolve(self.project,'edit/output.bin')
        self.a.execute('UPDATE execution_leases SET expires=0 WHERE run_id=?',(task,))
        second=cb.claim(task,'agent')
        with first.bind(),self.assertRaises(LeaseLost):self.files_a.publish(self.project,'edit/output.bin',uploaded['id'])
        with second.bind():self.files_b.publish(self.project,'edit/output.bin',uploaded['id'],expected_revision=0)
        second.release()

    def test_revision_and_project_boundaries(self):
        value=self.files_a.upload(self.project,self.path)
        other=self.a.add_project('other','other')['id']
        with self.assertRaises(ValueError):self.files_b.open(other,value['id']).__enter__()
        self.files_a.publish(self.project,'edit/result.bin',value['id'],expected_revision=0)
        with self.assertRaises(ValueError):self.files_b.publish(self.project,'edit/result.bin',value['id'],expected_revision=0)
        with self.assertRaises(ValueError):self.files_b.publish(self.project,'../outside',value['id'])

    def test_corruption_rejected_and_no_partial_target(self):
        value=self.files_a.upload(self.project,self.path)
        record=self.files_a.record(self.project,value['id'])
        self.backend.client.put_object(Bucket=self.backend.bucket,Key=record['object_key'],Body=b'corrupt')
        target=self.b.root/'verified.bin'
        with self.assertRaises(ValueError):self.files_b.materialize(self.project,value['id'],target)
        self.assertFalse(target.exists())

    def test_gc_cannot_remove_published_or_republish_deleting_object(self):
        published=self.files_a.upload(self.project,self.path)
        staged=self.files_a.upload(self.project,self.path)
        self.files_a.publish(self.project,'edit/kept.bin',published['id'])
        self.a.execute('UPDATE artifact_objects SET expires=0 WHERE project=?',(self.project,))
        self.assertEqual(self.files_b.collect_garbage(),1)
        self.assertTrue(self.files_b.materialize(self.project,published['id']).exists())
        with self.assertRaises(ValueError):self.files_a.publish(self.project,'edit/gone.bin',staged['id'])

    def test_replaced_object_is_retained_while_any_reference_exists(self):
        first=self.files_a.upload(self.project,self.path)
        self.files_a.publish(self.project,'edit/current.bin',first['id'])
        self.files_a.publish(self.project,'versions/old.bin',first['id'])
        self.path.write_bytes(b'new version');second=self.files_a.upload(self.project,self.path)
        self.files_b.publish(self.project,'edit/current.bin',second['id'],expected_revision=1)
        self.assertEqual(self.files_a.record(self.project,first['id'])['state'],'published')
        self.files_a.release_prefix(self.project,'versions')
        self.a.execute('UPDATE artifact_objects SET expires=0 WHERE id=?',(first['id'],))
        self.files_b.collect_garbage()
        self.assertEqual(self.files_b.materialize(self.project,second['id']).read_bytes(),b'new version')
        with self.assertRaises(ValueError):self.files_a.record(self.project,first['id'])

    def test_cancel_between_upload_and_publication_keeps_object_unpublished_and_collectable(self):
        from webapp.task_lifecycle import cancel_task
        task=agent_store.add_turn(self.a,self.project,'plan','cancel upload')
        lease=Coordinator(self.a,uuid.uuid4().hex,'test').claim(task,'agent')
        try:
            with lease.bind():uploaded=self.files_a.upload(self.project,self.path)
            cancel_task(self.b,task)
            with lease.bind(),self.assertRaises(LeaseLost):self.files_a.publish(self.project,'edit/late.bin',uploaded['id'])
            with self.assertRaises(FileNotFoundError):self.files_b.resolve(self.project,'edit/late.bin')
            self.a.execute('UPDATE artifact_objects SET expires=0 WHERE id=?',(uploaded['id'],))
            self.assertGreaterEqual(self.files_b.collect_garbage(),1)
        finally:lease.release()

    def test_verified_cache_reuses_hash_and_detects_local_tampering(self):
        from unittest.mock import patch
        first=self.files_a.upload(self.project,self.path);second=self.files_a.upload(self.project,self.path)
        cache=self.files_b.materialize(self.project,first['id'])
        with patch.object(self.backend,'get',side_effect=AssertionError('must reuse verified cache')):
            self.assertEqual(self.files_b.materialize(self.project,second['id']),cache)
        cache.write_bytes(b'corrupt')
        self.assertEqual(self.files_b.materialize(self.project,first['id']).read_bytes(),self.path.read_bytes())

    def test_partial_storage_failure_never_publishes_and_reservation_is_gc_safe(self):
        from unittest.mock import patch
        original=self.backend.put
        def accepted_then_lost(key,source):original(key,source);raise ConnectionError('lost storage response')
        with patch.object(self.backend,'put',side_effect=accepted_then_lost),self.assertRaises(ConnectionError):
            self.files_a.upload(self.project,self.path)
        self.assertFalse(self.a.query('SELECT * FROM artifact_files WHERE project=?',(self.project,)))
        self.a.execute('UPDATE artifact_objects SET expires=0 WHERE project=?',(self.project,))
        self.assertGreaterEqual(self.files_b.collect_garbage(),1)


if __name__=='__main__':unittest.main()
