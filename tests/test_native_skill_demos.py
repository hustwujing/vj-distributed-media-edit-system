import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from webapp import build_native_skill_demos as builder


class NativeSkillDemoTests(unittest.TestCase):
    def test_specs_match_the_frozen_skill_manifest(self):
        manifest=json.loads((Path(__file__).parents[1]/'runtime/skills/manifest.json').read_text())
        skills={item['id'] for item in manifest['skills']}
        self.assertEqual(set(builder.SPECS),skills);self.assertEqual(len(skills),25)
        for identifier,spec in builder.SPECS.items():
            with self.subTest(skill=identifier):
                self.assertTrue(spec[3].startswith('使用已安装的 `'+identifier+'` Native Skill'))
                self.assertTrue(spec[4]);self.assertEqual(len(spec[4]),len(set(spec[4])))

    def test_builder_adds_25_distinct_skills_and_five_media_cases(self):
        manifest=json.loads(builder.MANIFEST.read_text())
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);(root/'catalog.json').write_text(json.dumps([
                {'id':'legacy-'+str(index),'title':'Legacy','files':[],'sources':[]} for index in range(44)]))
            with patch.object(builder,'ROOT',root):builder.main()
            catalog=json.loads((root/'catalog.json').read_text())
        self.assertEqual(len(catalog),74);self.assertEqual(len({item['id'] for item in catalog}),74)
        native=[item for item in catalog if item.get('native_skill')]
        self.assertEqual({item['native_skill'] for item in native},{item['id'] for item in manifest['skills']})
        self.assertTrue(all(item['id']=='skill-'+item['native_skill'] for item in native))
        media=[item for item in catalog if item.get('media_generation')]
        self.assertEqual(len(media),5)
        combined=next(item for item in media if item['id']==builder.MEDIA_ID)
        self.assertEqual(combined['expected_outputs'],['image.png','video.mp4','narration.mp3'])
        self.assertEqual({item['id'] for item in media if item['id']!=builder.MEDIA_ID},set(builder.REFERENCE_MEDIA_IDS))


if __name__=='__main__':unittest.main()
