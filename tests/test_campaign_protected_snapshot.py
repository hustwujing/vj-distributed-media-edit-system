import json,sys,tempfile,unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
from deploy.common import env_file
from tests import campaign_dynamic_probe
with patch.dict(sys.modules,{'campaign_dynamic_probe':campaign_dynamic_probe}):
    from tests.campaign_dynamic_faults import protected_snapshot


class ProtectedSnapshotTests(unittest.TestCase):
    def setUp(self):
        temp=tempfile.TemporaryDirectory();self.addCleanup(temp.cleanup);base=Path(temp.name)
        self.root=base/'source';self.source=self.root/'.cluster/releases/frozen';self.source.mkdir(parents=True)
        self.config=base/'source-config';self.config.mkdir();self.own=base/'sandbox';self.own.mkdir()
        self.calls=[];self.pid='100'
        self.runtime=SimpleNamespace(REPO=self.own,ROOT=self.own/'.sandbox-cluster',CONF=base/'own-config',
            NAMESPACE='sandbox-agent',DATABASE='sandbox_db',env_file=env_file,run=self.run_readonly)
        self.record={'namespace':'source-agent','source':str(self.source),'config_root':str(self.config),'sha256':'a'*64}
        (self.root/'.cluster/release.json').write_text(json.dumps(self.record))
        for folder in (self.root,self.config):
            for name in ('.env','.agent.env','.storage.env','.distributed.env'):
                value='VIDEO_USE_DATABASE_URL=postgresql://source:not-a-real-password@127.0.0.1:15432/source_db\n' if name=='.distributed.env' else 'CONFIG=original\n'
                (folder/name).write_text(value)

    def run_readonly(self,args):
        self.assertEqual(args[:2],['systemctl','show']);self.assertTrue(args[2].startswith('source-agent-'))
        self.calls.append(args)
        return SimpleNamespace(stdout='MainPID='+self.pid+'\nActiveState=active\nExecMainStartTimestamp=frozen\n')

    def test_snapshot_reads_actual_target_without_leaking_credentials(self):
        result=protected_snapshot(self.root,self.runtime)
        self.assertEqual(result['database'],'source_db');self.assertEqual(len(result['units']),10)
        self.assertNotIn('not-a-real-password',json.dumps(result));self.assertEqual(len(self.calls),10)

    def test_before_after_detects_configuration_and_process_changes(self):
        before=protected_snapshot(self.root,self.runtime)
        (self.config/'.agent.env').write_text('CONFIG=changed\n');self.pid='200'
        after=protected_snapshot(self.root,self.runtime)
        self.assertNotEqual(before['config_hashes'],after['config_hashes'])
        self.assertNotEqual(before['units'],after['units'])
        self.assertEqual(before['workspace_config_hashes'],after['workspace_config_hashes'])

    def test_own_deployment_and_unsafe_namespace_are_rejected_before_service_reads(self):
        with self.assertRaises(ValueError):protected_snapshot(self.own,self.runtime)
        self.record['namespace']='../sandbox-agent';(self.root/'.cluster/release.json').write_text(json.dumps(self.record))
        with self.assertRaises(ValueError):protected_snapshot(self.root,self.runtime)
        self.assertEqual(self.calls,[])
