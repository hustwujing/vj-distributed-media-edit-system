"""Registered skill content is actually supplied and included in frozen policies."""
import hashlib
from pathlib import Path
import tempfile
import unittest
from dataclasses import replace
from unittest.mock import patch
from webapp import specialist_profiles as profiles
from webapp.workflow_agents import model_instructions

class ProfileSkillTests(unittest.TestCase):
 def test_registered_content_is_injected_and_content_changes_change_the_policy(self):
  profile=profiles.get('editing_implementer')
  with tempfile.TemporaryDirectory() as directory,patch.object(profiles,'SKILL_ROOT',Path(directory)),patch.dict(profiles.SKILL_FILES,{'media-editing':'reference.md'},clear=True):
   path=Path(directory)/'reference.md';path.write_text('Use actual bounded local evidence. VERSION_ONE')
   first=profile.snapshot();self.assertIn('VERSION_ONE',model_instructions(profile))
   self.assertEqual(first['skill_digests']['media-editing'],hashlib.sha256(path.read_bytes()).hexdigest())
   path.write_text('Use actual bounded local evidence. VERSION_TWO')
   self.assertNotEqual(first,profile.snapshot());self.assertIn('VERSION_TWO',model_instructions(profile))
   self.assertEqual(first['action_capabilities'],profile.snapshot()['action_capabilities'],'Skill content cannot change the tool grant')
 def test_unregistered_skill_is_rejected_at_registration(self):
  with self.assertRaisesRegex(ValueError,'skill'):
   profiles.register(replace(profiles.get('editing_implementer'),id='untrusted_skill_test',skills=('user-selected-file',)))
 def test_registered_skill_cannot_escape_its_root(self):
  with tempfile.TemporaryDirectory() as directory,patch.object(profiles,'SKILL_ROOT',Path(directory)),patch.dict(profiles.SKILL_FILES,{'media-editing':'../outside.md'},clear=True):
   with self.assertRaisesRegex(ValueError,'escaped'):profiles.get('editing_implementer').snapshot()
