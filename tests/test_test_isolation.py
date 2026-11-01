import os
from pathlib import Path
import socket
import tempfile
import unittest
from unittest.mock import patch
from tests.isolation import isolate_environment,temporary_root


class TestEnvironmentTests(unittest.TestCase):
    def test_loaded_deployment_configuration_cannot_redirect_regression_database_or_storage(self):
        from webapp.store import Store
        with tempfile.TemporaryDirectory() as folder,patch.dict(os.environ,{
            'VIDEO_USE_DATABASE_URL':'postgresql://production.invalid/production',
            'VIDEO_USE_DISTRIBUTED':'1','VIDEO_USE_OBJECT_BUCKET':'production',
            'VIDEO_USE_OBJECT_ENDPOINT':'https://production.invalid',
            'VIDEO_USE_OBJECT_ENCRYPTION_KEY':'private-key',
            'AWS_ACCESS_KEY_ID':'deployment-key','AWS_SECRET_ACCESS_KEY':'deployment-secret',
            'PGHOST':'production.invalid','ELEVENLABS_API_KEY':'deployment-key',
            'VIDEO_USE_TEST_REAL_MODELS':'1','VIDEO_USE_GPU_DEVICES':'3',
        }):
            isolate_environment(Path(folder))
            self.assertEqual(temporary_root(),Path(folder)/'tmp')
            self.assertTrue(temporary_root().is_dir())
            store=Store(Path(folder)/'test')
            self.assertEqual(store.dsn,'')
            self.assertFalse(store.shared_files)
            self.assertNotIn('AWS_SECRET_ACCESS_KEY',os.environ)
            self.assertNotIn('PGHOST',os.environ)
            self.assertNotIn('ELEVENLABS_API_KEY',os.environ)
            self.assertEqual(os.environ['VIDEO_USE_TEST_REAL_MODELS'],'1')
            self.assertEqual(os.environ['VIDEO_USE_GPU_DEVICES'],'3')
            self.assertEqual(os.environ['AWS_EC2_METADATA_DISABLED'],'true')

    @unittest.skipUnless(os.environ.get('VIDEO_USE_TEST_NETWORK_ISOLATED')=='1','Run through the isolated Linux suite entrypoint')
    def test_regression_network_exposes_only_loopback(self):
        self.assertEqual({name for _,name in socket.if_nameindex()},{'lo'})


if __name__=='__main__':unittest.main()
