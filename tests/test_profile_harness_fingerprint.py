import os
from pathlib import Path
import shutil
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import Mock,patch

from webapp import profile_harnesses


class ProfileHarnessFingerprintTests(unittest.TestCase):
    def test_every_python_module_changes_frozen_identity_including_preserved_mtime(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory)/'webapp'
            shutil.copytree(Path(profile_harnesses.__file__).parent,root,ignore=shutil.ignore_patterns('__pycache__','static'))
            with patch.object(profile_harnesses,'__file__',str(root/'profile_harnesses.py')):
                harness=profile_harnesses.get('structured-profile-v4');baseline=harness.fingerprint()
                sources=sorted(root.rglob('*.py'))
                self.assertIn(root/'usage_protocol.py',sources)
                self.assertIn(root/'safe_stream.py',sources)
                for path in sources:
                    with self.subTest(module=path.name):
                        original=path.read_bytes();stat=path.stat()
                        path.write_bytes(original+b'\n# execution policy changed\n')
                        os.utime(path,ns=(stat.st_atime_ns,stat.st_mtime_ns))
                        self.assertNotEqual(harness.fingerprint(),baseline)
                        path.write_bytes(original)
                        self.assertEqual(harness.fingerprint(),baseline)
                target=root/'usage_protocol.py';original=target.read_bytes();stat=target.stat()
                target.write_bytes(original.replace(b'output_tokens',b'output_tokenX',1))
                os.utime(target,ns=(stat.st_atime_ns,stat.st_mtime_ns))
                self.assertNotEqual(harness.fingerprint(),baseline,'same-size edit must invalidate cached digest')

    def test_new_nested_module_and_removal_change_identity_but_noncode_does_not(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);(root/'profile_harnesses.py').write_text('# entry\n')
            with patch.object(profile_harnesses,'__file__',str(root/'profile_harnesses.py')):
                harness=profile_harnesses.get('structured-profile-v4');baseline=harness.fingerprint()
                nested=root/'new_engine';nested.mkdir();module=nested/'policy.py';module.write_text('# policy\n')
                self.assertNotEqual(harness.fingerprint(),baseline)
                module.unlink();self.assertEqual(harness.fingerprint(),baseline)
                (root/'page.html').write_text('presentation');self.assertEqual(harness.fingerprint(),baseline)

    def test_changed_dependency_rejects_old_profile_before_compute_or_model_call(self):
        from webapp.specialist_profiles import get
        from webapp.workflow_agents import execute
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);(root/'profile_harnesses.py').write_text('# entry\n')
            dependency=root/'safe_stream.py';dependency.write_text('# initial policy\n')
            with patch.object(profile_harnesses,'__file__',str(root/'profile_harnesses.py')):
                profile=get('media_planner');task={'spec':{'profile':profile.id},'profile':profile.snapshot()}
                dependency.write_text('# changed policy\n');lease=Mock()
                with patch('webapp.workflow_agents.model') as model:
                    with self.assertRaisesRegex(ValueError,'replan is required'):
                        execute(SimpleNamespace(store=Mock()),lease,{},task)
                    lease.start_compute.assert_not_called();model.assert_not_called()
