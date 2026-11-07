import unittest
from pathlib import Path
import tempfile
from unittest.mock import patch
from webapp.deployment_identity import namespace,socket_root,cgroup_member


class DeploymentIdentityTests(unittest.TestCase):
    def test_private_environment_roundtrip_preserves_literal_secrets(self):
        from deploy import common
        with tempfile.TemporaryDirectory() as directory:
            path=Path(directory)/'private.env'
            values={'KEY':'literal $(command) `value` with spaces','EMPTY':''}
            common.private_env(path,values)
            self.assertEqual(common.env_file(path),values)
            self.assertEqual(path.stat().st_mode&0o777,0o600)
    def test_dynamic_node_identity_ranges_do_not_overlap(self):
        from deploy import dynamic_cluster as cluster
        self.assertNotEqual(cluster.registry('a'),cluster.registry('b'))
        self.assertNotEqual(cluster.data('node','a'),cluster.data('node','b'))
        self.assertGreaterEqual(abs(cluster.UIDS['a']-cluster.UIDS['b']),64)
    def test_shared_database_restart_does_not_stop_private_services(self):
        from deploy import dynamic_cluster as cluster
        from deploy import systemd_templates
        config=cluster.template_config(cluster.REPO)
        for factory,owned in [(systemd_templates.api_unit,'dynamic-agent-media@%i.service'),
                              (systemd_templates.worker_unit,'dynamic-agent-watchdog@%i.service'),
                              (systemd_templates.reaper_unit,None)]:
            with self.subTest(unit=factory.__name__):
                text=factory(config)
                properties={line.split('=',1)[0]:line.split('=',1)[1].split() for line in text.splitlines() if '=' in line}
                self.assertNotIn('video-use-db.service',properties.get('Requires',[]))
                self.assertIn('video-use-db.service',properties['Wants'])
                self.assertIn('video-use-db.service',properties['After'])
                if owned:self.assertIn(owned,properties['Requires'])
    def test_dynamic_units_use_explicit_private_deployment_values(self):
        from deploy import dynamic_cluster as cluster
        from deploy import systemd_templates
        config=cluster.template_config(cluster.REPO)
        units=[systemd_templates.api_unit(config),systemd_templates.worker_unit(config),
               systemd_templates.reaper_unit(config),systemd_templates.media_unit(config),
               systemd_templates.observer_unit(config)]
        for unit in units:
            self.assertIn('LogNamespace=dynamic-agent',unit)
            self.assertNotIn('/etc/video-use-cluster',unit)
            self.assertNotIn('/run/video-use-agent',unit)
        self.assertIn('--root-path /distributed-agent-system',units[0])
        self.assertIn('RuntimeDirectory=dynamic-agent-agent-%i',units[1])
        self.assertIn('RuntimeDirectory=dynamic-agent-agent-%i',units[2])
        self.assertIn('RuntimeDirectory=da-media-%i',units[3])
        self.assertIn('--port 19768',units[4])
        proxy=cluster.nginx_config()
        self.assertIn('127.0.0.1:19766',proxy);self.assertIn('127.0.0.1:19767',proxy)
        self.assertNotIn('http_503',proxy)
    def test_private_namespace_cannot_reap_main_cgroups(self):
        with patch.dict('os.environ',{'VIDEO_USE_DEPLOYMENT':'distributed-agent'}):
            self.assertEqual(str(socket_root()),'/run/distributed-agent-agent')
            self.assertEqual(cgroup_member('/sys/fs/cgroup/memory/distributed-agent/task'),'/distributed-agent/task')
            with self.assertRaises(ValueError):cgroup_member('/sys/fs/cgroup/memory/video-use/task')
            with self.assertRaises(ValueError):cgroup_member('/sys/fs/cgroup/distributed-agent/../video-use/task')
    def test_invalid_names_do_not_become_paths(self):
        for name in ('../video-use','/root','name space','a'*32):
            with self.subTest(name=name),patch.dict('os.environ',{'VIDEO_USE_DEPLOYMENT':name}),self.assertRaises(ValueError):namespace()
    def test_node_sockets_are_separate(self):
        with patch.dict('os.environ',{'VIDEO_USE_DEPLOYMENT':'dynamic-agent','VIDEO_USE_NODE_INSTANCE':'a'}):a=socket_root()
        with patch.dict('os.environ',{'VIDEO_USE_DEPLOYMENT':'dynamic-agent','VIDEO_USE_NODE_INSTANCE':'b'}):b=socket_root()
        self.assertNotEqual(a,b)
        with patch.dict('os.environ',{'VIDEO_USE_NODE_INSTANCE':'../main'}),self.assertRaises(ValueError):socket_root()
    def test_fresh_database_initializes_object_metadata_before_orchestrator_grants(self):
        import inspect
        from deploy import dynamic_cluster
        source=inspect.getsource(dynamic_cluster.initialize)
        self.assertLess(source.index('s.artifacts'),source.index('GRANT SELECT,INSERT,UPDATE,DELETE'))
        self.assertLess(source.index('image_api.initialize(s)'),source.index('GRANT SELECT,INSERT,UPDATE,DELETE'))

    def test_dynamic_release_freezes_model_configuration_and_units_use_the_snapshot(self):
        from deploy import dynamic_cluster as cluster
        from deploy import systemd_templates
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);repo=root/'repo';conf=root/'conf';repo.mkdir();conf.mkdir()
            for name,value in {'.env':'BASE=one\n','.agent.env':'VIDEO_USE_AGENT_PROFILE=claude\n',
                               '.storage.env':'VIDEO_USE_OBJECT_BUCKET=bucket\n','.distributed.env':'VIDEO_USE_DEPLOYMENT=dynamic-agent\n'}.items():
                (repo/name).write_text(value)
            with patch.object(cluster,'REPO',repo),patch.object(cluster,'CONF',conf):
                snapshot,digest=cluster.snapshot_configuration()
                (repo/'.agent.env').write_text('VIDEO_USE_AGENT_PROFILE=changed\n')
                self.assertEqual(cluster.env_file(snapshot/'.agent.env')['VIDEO_USE_AGENT_PROFILE'],'claude')
                self.assertNotEqual(digest,cluster.snapshot_configuration()[1])
                unit=systemd_templates.worker_unit(cluster.template_config(root/'release',snapshot))
                self.assertIn('EnvironmentFile='+str(snapshot/'.agent.env'),unit)

    def test_release_record_is_atomic_and_partial_release_cannot_restart(self):
        from deploy import dynamic_cluster as cluster
        with tempfile.TemporaryDirectory() as directory,patch.object(cluster,'ROOT',Path(directory)):
            cluster.write_release({'source':'/release','state':'deploying'})
            self.assertEqual(cluster.release_record()['state'],'deploying')
            self.assertFalse((Path(directory)/'release.new').exists())
            with self.assertRaisesRegex(ValueError,'incomplete'):
                cluster.restart_release()

    def test_deploy_journal_precedes_schema_reset_and_unit_switch(self):
        import inspect
        from deploy import dynamic_cluster
        source=inspect.getsource(dynamic_cluster.deploy)
        self.assertLess(source.index('write_release(record)'),source.index('reset_database()'))
        self.assertLess(source.index('write_release(record)'),source.index('install_units(source,config_root)'))
        self.assertLess(source.index("'state':'deploying'"),source.index("'state':'ready'"))

    def test_full_gate_evidence_promotes_over_targeted_report_for_identical_source(self):
        import json
        from deploy import dynamic_cluster as cluster
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);source=root/'release';repo=root/'repo'
            (source/'docs/test-evidence').mkdir(parents=True);(repo/'docs/test-evidence').mkdir(parents=True)
            targeted={'passed':True,'selection':['tests.test_dynamic_graph'],'source_sha256':'same'}
            complete={'passed':True,'selection':['all Python tests'],'source_sha256':'same'}
            (source/'docs/test-evidence/distributed-suite.json').write_text(json.dumps(targeted))
            (repo/'docs/test-evidence/distributed-suite.json').write_text(json.dumps(complete))
            with patch.object(cluster,'REPO',repo),patch.object(cluster.common,'digest',return_value='same'), \
                    patch.object(cluster,'validate_isolation'),patch.object(cluster,'environment',return_value={}):
                cluster.gate(source)
            self.assertEqual(json.loads((source/'docs/test-evidence/distributed-suite.json').read_text()),complete)
