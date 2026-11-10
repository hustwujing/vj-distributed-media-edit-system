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
 def test_latency_sensitive_profiles_require_bounded_action_progression(self):
  diagnostician=profiles.PROFILES['media_failure_diagnostician'].prompt
  implementer=profiles.PROFILES['editing_implementer'].prompt
  self.assertIn('下一轮必须立即调用result',diagnostician)
  self.assertIn('每个数组最多5项',diagnostician)
  self.assertIn('检查实现所必需的输入后立即调用script',implementer)
  self.assertIn('下一轮立即调用result',implementer)
  for identifier in ('visual_quality_reviewer','delivery_reviewer'):
   reviewer=profiles.PROFILES[identifier].prompt
   self.assertIn('下一轮必须立即调用result',reviewer)
   self.assertIn('用长篇推理耗尽输出预算',reviewer)
   self.assertEqual(profiles.PROFILES[identifier].max_inspection_rounds,2)
   self.assertEqual(profiles.PROFILES[identifier].finalize_rounds,3 if identifier=='delivery_reviewer' else 2)
  self.assertIn('criterion_index必须恰好覆盖',profiles.PROFILES['delivery_reviewer'].prompt)
  self.assertEqual(profiles.PROFILES['media_failure_diagnostician'].max_inspection_rounds,1)

 def test_repair_prompts_require_evidence_first_and_measured_layouts(self):
  manager=profiles.get('media_planner').prompt
  implementer=profiles.get('editing_implementer').prompt
  self.assertIn('audio_envelope',manager);self.assertIn('review_again',manager)
  self.assertIn('不是用户契约',manager);self.assertIn('不要为放弃自己选择的实现再次询问用户',manager)
  self.assertIn('先给方案',manager);self.assertIn('不得用respond结束',manager)
  self.assertIn('不得再创建media.analyze业务Task',manager)
  self.assertIn('textbbox',implementer);self.assertIn('可见内容bbox',implementer)
  structured=model_instructions(profiles.get('editing_implementer'))
  self.assertIn('VIDEO_USE_STEP_SCRATCH',structured)
  self.assertIn('/workspace 是稳定Shell工作目录',structured)

 def test_every_script_author_and_diagnostician_receives_the_executable_runtime_contract(self):
  import os,re
  for identifier in ('media_planner','editing_implementer','media_failure_diagnostician'):
   with self.subTest(profile=identifier),tempfile.TemporaryDirectory() as directory:
    profile=profiles.get(identifier);instructions=model_instructions(profile)
    contract=next(item for item in profiles.skill_materials(profile) if item['id']=='script-runtime')
    self.assertIn(contract['text'],instructions)
    code=re.search(r'```python\n(.*?)\n```',contract['text'],re.S)[1]
    source=Path(directory)/'input.png';source.write_bytes(b'fixture')
    output=Path(directory)/'output';namespace={}
    import json
    with patch.dict(os.environ,{'VIDEO_USE_STEP_INPUTS':json.dumps({'source':[str(source)]}),'VIDEO_USE_STEP_OUTPUT':str(output)}):exec(code,namespace)
    self.assertEqual(namespace['source'],source);self.assertEqual(namespace['output'],output);self.assertTrue(output.is_dir())

 def test_registered_content_is_injected_and_content_changes_change_the_policy(self):
  profile=replace(profiles.get('editing_implementer'),skills=('media-editing',))
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
   with self.assertRaisesRegex(ValueError,'escaped'):replace(profiles.PROFILES['editing_implementer'],skills=('media-editing',)).snapshot()
