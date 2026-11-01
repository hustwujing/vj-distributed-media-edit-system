import importlib.util
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

spec=importlib.util.spec_from_file_location('cluster_deployment',Path(__file__).resolve().parents[1]/'deploy/cluster.py')
cluster=importlib.util.module_from_spec(spec);spec.loader.exec_module(cluster)


class DeploymentTests(unittest.TestCase):
    def setUp(self):
        # Deployment commands use a module-level root. Keep maintenance markers
        # away from the actual installed cluster even when methods are mocked.
        temporary=tempfile.TemporaryDirectory(prefix='vu-deploy-unit-')
        self.addCleanup(temporary.cleanup)
        root=patch.object(cluster,'ROOT',Path(temporary.name))
        root.start();self.addCleanup(root.stop)

    def test_private_environment_roundtrip_preserves_literal_secrets(self):
        with tempfile.TemporaryDirectory() as directory:
            path=Path(directory)/'private.env'
            values={'KEY':'literal $(command) `value` with spaces','EMPTY':''}
            cluster.private_env(path,values)
            self.assertEqual(cluster.env_file(path),values)
            self.assertEqual(path.stat().st_mode&0o777,0o600)

    def test_node_identity_and_data_ranges_do_not_overlap(self):
        self.assertNotEqual(cluster.registry('a'),cluster.registry('b'))
        self.assertNotEqual(cluster.data('node','a'),cluster.data('node','b'))
        self.assertGreaterEqual(abs(cluster.UIDS['a']-cluster.UIDS['b']),64)
        self.assertFalse(set(cluster.GPUS['a'].split(','))&set(cluster.GPUS['b'].split(',')))

    def test_stop_preserves_watchdogs_and_database_when_cleanup_is_incomplete(self):
        calls=[]
        with patch.object(cluster,'active',return_value=True),patch.object(cluster,'proxy'),patch.object(cluster,'parallel_service',side_effect=lambda action,names:calls.append((action,names))),patch.object(cluster,'wait_clean',side_effect=RuntimeError('pending')),patch.object(cluster,'run') as run:
            with self.assertRaises(RuntimeError):cluster.stop()
            self.assertEqual(calls,[('stop',cluster.APIS+cluster.WORKERS)])
            run.assert_not_called()

    def test_stop_orders_api_worker_then_cleanup_then_database(self):
        calls=[]
        with patch.object(cluster,'active',return_value=True),patch.object(cluster,'proxy'),patch.object(cluster,'parallel_service',side_effect=lambda action,names:calls.append(names)),patch.object(cluster,'wait_clean',side_effect=lambda:calls.append('clean')),patch.object(cluster,'run',side_effect=lambda args:calls.append(args)):
            cluster.stop()
        self.assertEqual(calls,[cluster.APIS+cluster.WORKERS,'clean',cluster.REAPERS+cluster.MEDIA,['systemctl','stop',cluster.DB_SERVICE]])

    def test_proxy_does_not_retry_non_idempotent_posts(self):
        self.assertNotIn('non_idempotent',cluster.nginx_config())
        self.assertIn('18766',cluster.nginx_config());self.assertIn('18767',cluster.nginx_config())
        self.assertIn('return 503',cluster.nginx_config(maintenance=True))

    def test_service_configuration_uses_private_inputs_and_distinct_roots(self):
        for value in (cluster.api_unit(),cluster.worker_unit(),cluster.reaper_unit()):
            self.assertIn('/.storage.env',value);self.assertIn('/.distributed.env',value)
            self.assertNotIn('AWS_SECRET_ACCESS_KEY=',value)
        self.assertIn('node-%i',cluster.worker_unit())
        self.assertIn('/var/lib/vu-%i',cluster.reaper_unit())
        self.assertIn('15432',str(cluster.DB_PORT))
