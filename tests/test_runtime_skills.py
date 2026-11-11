import importlib.util
import json
from pathlib import Path
import tempfile
import unittest

from webapp.runtime_skills import materialize_skills


ROOT=Path(__file__).resolve().parents[1]


class RuntimeSkillTests(unittest.TestCase):
    def test_frozen_manifest_matches_all_imported_skills(self):
        spec=importlib.util.spec_from_file_location('runtime_skill_bundle',ROOT/'runtime/skill_bundle.py')
        module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
        expected=json.loads((ROOT/'runtime/skills/manifest.json').read_text())
        self.assertEqual(module.expanded(),expected);self.assertEqual(len(expected['skills']),25)
        self.assertEqual({row['id'] for row in expected['skills']},
            {path.parent.name for path in (ROOT/'runtime/skills').glob('*/SKILL.md')})

    def test_each_native_harness_gets_all_image_pinned_skills(self):
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder);destinations={name:root/name for name in
                ('codex','claude_code','deepseek_harness')}
            for harness,destination in destinations.items():
                result=materialize_skills(harness,source=ROOT/'runtime/skills',destinations=destinations)
                self.assertEqual((result['bundle_version'],result['skill_count']),('2026.11.11.1',25))
                self.assertEqual(len(list(destination.glob('*/SKILL.md'))),25)
                self.assertTrue((destination/'xlsx').is_symlink())

    def test_upgrade_removes_only_previous_image_managed_skill(self):
        with tempfile.TemporaryDirectory() as folder:
            destination=Path(folder)/'codex';destination.mkdir()
            (destination/'old-managed').mkdir();(destination/'custom').mkdir()
            (destination/'.image-bundle.json').write_text(json.dumps(
                {'bundle_version':'old','skills':['old-managed']}))
            materialize_skills('codex',source=ROOT/'runtime/skills',destinations={'codex':destination})
            self.assertFalse((destination/'old-managed').exists());self.assertTrue((destination/'custom').is_dir())


if __name__=='__main__':unittest.main()
