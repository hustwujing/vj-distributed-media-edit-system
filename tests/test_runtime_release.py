import json
from pathlib import Path
import tempfile
import unittest

from deploy.runtime_release import bundle_record,host_capacity,load_spec
from deploy.verify_runtime_images import docker_command
from deploy.runtime_security_report import license_summary,vulnerability_summary


class RuntimeReleaseTests(unittest.TestCase):
    def test_skill_bundle_digest_is_content_and_name_bound(self):
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder);(root/'a').write_text('one');(root/'b').write_text('two')
            first=bundle_record(root,['b','a'])
            self.assertEqual([row['path'] for row in first['files']],['a','b'])
            (root/'b').write_text('changed')
            self.assertNotEqual(first['digest'],bundle_record(root,['a','b'])['digest'])

    def test_checked_in_release_spec_is_consistent(self):
        value=load_spec()
        self.assertEqual(set(value['profiles']),
            {'command-v1','agent-codex-v1','agent-claude-v1','agent-deepseek-v1'})
        self.assertEqual(value['profiles']['agent-claude-v1']['model_routes'],
            ['/messages','/v1/messages'])
        self.assertEqual(value['profiles']['agent-deepseek-v1']['model_routes'],
            ['/messages','/v1/messages'])

    def test_targeted_selection_never_runs_or_claims_omitted_images(self):
        from unittest.mock import patch
        from types import SimpleNamespace
        from deploy.verify_runtime_images import verify
        manifest={'verification_scope':'targeted_only','images':{'agent-runtime-claude:build':{
            'user':'10001:10001','entrypoint':['/opt/venv/bin/python','-m','webapp.runtime_bootstrap'],
            'secret_marker_findings':[]}}}
        with patch('deploy.verify_runtime_images.subprocess.run',return_value=SimpleNamespace(returncode=0,stdout='',stderr='')) as run:
            report=verify('docker',manifest)
        self.assertEqual(run.call_count,1);self.assertEqual(set(report['images']),{'agent-runtime-claude:build'})
        self.assertEqual(report['verification_scope'],'targeted_only')

    def test_capacity_is_bounded_by_each_resource_and_configured_limit(self):
        gib=1024**3
        value=host_capacity(30,.5,cpus=128,memory_bytes=512*gib,disk_free_bytes=4*1024*gib)
        self.assertTrue(value['requested_is_feasible']);self.assertEqual(value['supported_resident'],30)
        constrained=host_capacity(30,1,cpus=8,memory_bytes=16*gib,disk_free_bytes=100*gib)
        self.assertFalse(constrained['requested_is_feasible'])
        self.assertEqual(constrained['supported_resident'],2)

    def test_contract_runner_is_networkless_read_only_and_unprivileged(self):
        command=docker_command('docker','image:build','true')
        self.assertIn('none',command);self.assertIn('--read-only',command)
        self.assertEqual(command[command.index('--cap-drop')+1],'ALL')
        self.assertIn('no-new-privileges',command)

    def test_security_summary_deduplicates_and_prioritizes_fixes(self):
        match={'vulnerability':{'id':'CVE-1','severity':'High','fix':{'versions':['2']}},
            'artifact':{'name':'pkg','version':'1','type':'python'}}
        result=vulnerability_summary({'matches':[match,match],'descriptor':{}})
        self.assertEqual(result['raw_matches'],2);self.assertEqual(result['unique_package_vulnerabilities'],1)
        self.assertEqual(result['fixable_severity'],{'High':1})
        licenses=license_summary({'components':[{'licenses':[{'license':{'id':'MIT'}}]},{'name':'unknown'}]})
        self.assertEqual(licenses['declared_licenses'],{'MIT':1})
        self.assertEqual(licenses['components_without_declared_license'],1)

    def test_security_summary_binds_only_selected_built_images(self):
        from deploy.runtime_security_report import build
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder);manifest=root/'manifest.json'
            manifest.write_text(json.dumps({'verification_scope':'targeted_only',
                'images':{'agent-runtime-claude:build':{'local_image_id':'sha256:fixture'}}}))
            for suffix,value in [('cdx.json',{'components':[]}),('spdx.json',{}),('grype.json',{'matches':[]})]:
                (root/('agent-runtime-claude.'+suffix)).write_text(json.dumps(value))
            report=build(root,root,manifest=manifest)
            self.assertEqual(set(report['images']),{'agent-runtime-claude'})
            self.assertEqual(report['verification_scope'],'targeted_only')
            self.assertEqual(report['images']['agent-runtime-claude']['local_image_id'],'sha256:fixture')


if __name__=='__main__':unittest.main()
