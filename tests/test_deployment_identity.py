import unittest
from unittest.mock import patch
from webapp.deployment_identity import namespace,socket_root,cgroup_member


class DeploymentIdentityTests(unittest.TestCase):
    def test_shared_database_restart_does_not_stop_private_services(self):
        from deploy import dynamic_cluster as cluster
        for factory,owned in [(cluster.templates.api_unit,'dynamic-agent-media@%i.service'),
                              (cluster.templates.worker_unit,'dynamic-agent-watchdog@%i.service'),
                              (cluster.templates.reaper_unit,None)]:
            with self.subTest(unit=factory.__name__):
                text=cluster.unit_text(factory())
                properties={line.split('=',1)[0]:line.split('=',1)[1].split() for line in text.splitlines() if '=' in line}
                self.assertNotIn('video-use-db.service',properties.get('Requires',[]))
                self.assertIn('video-use-db.service',properties['Wants'])
                self.assertIn('video-use-db.service',properties['After'])
                if owned:self.assertIn(owned,properties['Requires'])
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
