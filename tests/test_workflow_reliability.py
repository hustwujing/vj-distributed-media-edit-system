"""Generic recovery contracts: no production project identifiers or fixtures."""
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from webapp.text_formats import validate_subtitle,FormatError
from webapp.quality_policy import Acceptance,validate_typed_output_targets
from webapp.execution_contracts import classify_error
from webapp.artifacts import order_artifacts
from webapp import agent_store,workflows
from tests import test_workflows as fixtures

ASS='[Script Info]\nScriptType: v4.00+\n[Events]\nFormat: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text\nDialogue: 0,0:00:00.10,0:00:01.50,Default,,0,0,0,,你好, world\nDialogue: 1,0:00:00.30,0:00:01.00,Default,,0,0,0,,重叠字幕\n'
SRT='1\n00:00:00,100 --> 00:00:01,500\n你好, world\n'
VTT='WEBVTT\n\nNOTE sample\nignored\n\nchapter\n00:00.100 --> 00:01.500 align:start\n你好, world\n'

class FormatTests(unittest.TestCase):
    def check(self,root,files,rules):
        result=subprocess.run([sys.executable,str(Path(__file__).resolve().parents[1]/'webapp/quality_check.py')],cwd=root,
            input=json.dumps({'files':files,'acceptance':rules}),text=True,capture_output=True,timeout=20)
        self.assertEqual(result.returncode,0,result.stderr);return json.loads(result.stdout)
    def test_real_checker_accepts_text_and_format_specific_subtitles(self):
        with tempfile.TemporaryDirectory() as d:
            root=Path(d)
            for name,content in [('多语.ass',ASS),('legacy.SSA',ASS),('captions.srt',SRT),('captions.vtt',VTT)]:
                with self.subTest(name=name):
                    (root/name).write_text(content,encoding='utf-8-sig')
                    rules=[{'kind':'text','file':name,'contains':['你好']},{'kind':'subtitle','file':name}]
                    validate_typed_output_targets([Acceptance(**r) for r in rules],[name])
                    report=self.check(root,[name],rules)
                    self.assertTrue(report['passed'])
                    self.assertEqual(report['files'][0]['kind'],'subtitle')
    def test_subtitle_type_is_attested_without_optional_acceptance_rules(self):
        with tempfile.TemporaryDirectory() as d:
            root=Path(d)
            for name,content in [('captions.srt',SRT),('captions.vtt',VTT),('captions.ass',ASS),('captions.SSA',ASS)]:
                with self.subTest(name=name):
                    (root/name).write_text(content,encoding='utf-8-sig')
                    report=self.check(root,[name],[])
                    self.assertTrue(report['technical_valid']);self.assertTrue(report['passed'])
                    self.assertEqual(report['files'][0]['kind'],'subtitle')
                    self.assertEqual(report['files'][0]['format'],Path(name).suffix[1:].lower())
                    self.assertGreater(report['files'][0]['cues'],0)
    def test_invalid_subtitle_cannot_claim_a_trusted_input_type(self):
        with tempfile.TemporaryDirectory() as d:
            root=Path(d);(root/'fake.srt').write_text('ordinary text with a subtitle extension')
            report=self.check(root,['fake.srt'],[])
            self.assertFalse(report['technical_valid']);self.assertFalse(report['passed'])
            self.assertEqual(report['files'],[])
            self.assertEqual(report['checks'][0]['code'],'subtitle')
    def test_corrupt_subtitles_encoding_and_content_do_not_pass(self):
        with tempfile.TemporaryDirectory() as d:
            root=Path(d)
            for name,content,code in [('x.ass',ASS.replace('0:00:01.50','0:00:00.01').encode(),'subtitle'),
                                      ('x.srt',b'1\n00:70:00,000 --> 00:71:00,000\nx','subtitle'),
                                      ('x.vtt',b'00:00.000 --> 00:01.000\nx','subtitle'),
                                      ('x.ass',b'\xff\xfe','encoding'),('x.ass',b'abc\0def','encoding'),
                                      ('x.bin',b'valid text','unsupported_format')]:
                with self.subTest(name=name,code=code):
                    (root/name).write_bytes(content)
                    result=self.check(root,[name],[{'kind':'subtitle','file':name}])
                    self.assertFalse(result['passed'])
                    self.assertTrue(any(code==check.get('code') or code in check.get('codes',[]) for check in result['checks']))
            (root/'valid.ass').write_text(ASS)
            result=self.check(root,['valid.ass'],[{'kind':'text','file':'valid.ass','contains':['absent phrase']}])
            self.assertFalse(result['passed']);self.assertIn('contains',result['checks'][-1]['codes'])
    def test_unsupported_targets_fail_before_rendering(self):
        for kind,filename in [('text','movie.mp4'),('subtitle','notes.md'),('json','audio.wav')]:
            rule=Acceptance(kind=kind,file=filename,**({'contains':['x']} if kind=='text' else {}))
            with self.assertRaises(ValueError):validate_typed_output_targets([rule],[filename])
    def test_wildcard_text_rules_select_text_only(self):
        with tempfile.TemporaryDirectory() as d:
            root=Path(d);(root/'x.ass').write_text(ASS);(root/'unknown.dat').write_bytes(b'\xff')
            result=self.check(root,['x.ass','unknown.dat'],[{'kind':'text','file':'*','contains':['你好']}])
            self.assertTrue(result['passed']);self.assertEqual(len(result['checks']),1)
    def test_text_size_is_bounded(self):
        with tempfile.TemporaryDirectory() as d:
            root=Path(d);(root/'large.ass').write_bytes(b'x'*(2*1024**2+1))
            result=self.check(root,['large.ass'],[{'kind':'subtitle','file':'large.ass'}])
            self.assertFalse(result['passed']);self.assertIn('max_bytes',result['checks'][0]['codes'])

class RecoveryTests(unittest.TestCase):
    def setUp(self):
        self.f=fixtures.WorkflowTests();self.f.setUp();self.addCleanup(self.f.tearDown)
    def test_structured_parameter_failure_goes_directly_to_diagnosis(self):
        from webapp.workflow_runtime import StepFailure
        step=fixtures.local_step();step.update(retry_safe=True,on_failure='diagnose')
        self.f.submit([step])
        record={'schema':1,'event':'tool.failure','exception_type':'PlanError','error_code':'PlanError'}
        def fail(*args):raise StepFailure({'log_tail':json.dumps(record),'message':'unrelated localized text'})
        self.f.adapter.local=fail;self.f.tick();w=self.f.get()
        self.assertEqual(w['steps'][0]['attempts'],1);self.assertEqual(w['agent_review']['purpose'],'diagnose')
        self.assertEqual(classify_error({'log_tail':json.dumps(record)},'local'),'parameters')
        self.assertEqual(classify_error({'log_tail':json.dumps(record)},'submit'),'submission_unknown')
    def test_transient_failure_still_retries(self):
        from webapp.workflow_runtime import StepFailure
        self.f.submit([dict(fixtures.local_step(),retry_safe=True)])
        def fail(*args):raise StepFailure({'code':'timeout','message':'timed out'})
        self.f.adapter.local=fail;self.f.tick();w=self.f.get()
        self.assertEqual(w['steps'][0]['status'],'pending');self.assertEqual(w['steps'][0]['wait_reason'],'retry')
    def test_empty_model_result_diagnoses_once_and_cannot_resume_identical_input(self):
        from webapp.workflow_runtime import StepFailure
        step=fixtures.local_step('select');step.update(retry_safe=True,on_failure='diagnose')
        self.f.submit([step,fixtures.local_step('dependent',['select'])])
        record={'schema':1,'event':'tool.failure','exception_type':'ModelResultError'}
        def fail(*args):raise StepFailure({'log_tail':json.dumps(record)})
        self.f.adapter.local=fail;self.f.tick();w=self.f.get()
        self.assertEqual(w['steps'][0]['attempts'],1)
        self.assertEqual(w['steps'][0]['error_code'],'model_result')
        self.assertEqual(w['steps'][1]['attempts'],0)
        self.assertEqual(w['agent_review']['purpose'],'diagnose')
        self.assertFalse(workflows.public(w)['resumable'])
    def test_controlled_edit_rejects_paths_at_admission(self):
        from webapp.operation_parameters import Edit
        for plan in ({'sources':{'x':'/private'}},{'transcripts':{}},{'clips':[{'file':'arbitrary.mov'}]}):
            with self.assertRaises(ValueError):Edit(edit_plan=plan)
        Edit(edit_plan={'clips':[{'asset':'owned-id','words':[0,2]}]})
    def test_ancestor_context_survives_reopen_without_execution_authority(self):
        from webapp.continuation_context import snapshot
        from webapp.store import Store
        self.f.submit([fixtures.local_step()]);self.f.tick();workflows.cancel(self.f.store,self.f.turn)
        child=agent_store.add_turn(self.f.store,self.f.project,'plan','new decision',parent_task_id=self.f.turn)
        agent_store.finish(self.f.store,child,'failed',error='interrupted discussion')
        descendant=agent_store.add_turn(self.f.store,self.f.project,'plan','resume discussion',parent_task_id=child)
        other=self.f.store.add_project('another-owner','other')['id']
        restored=Store(self.f.store.root);context=snapshot(restored,self.f.project,descendant)
        self.assertEqual(context['parent_task_id'],child);self.assertEqual(context['source_task_id'],self.f.turn)
        self.assertEqual(context['workflow']['status'],'cancelled');self.assertFalse(context['execution_authorized'])
        self.assertEqual(context['workflow']['steps'][0]['revision'],0)
        self.assertIsNone(snapshot(restored,other,descendant)['workflow'])
        self.assertIsNone(workflows.get(restored,descendant))
        self.assertEqual(agent_store.turn(restored,descendant)['mode'],'plan')
    def test_step_success_is_not_final_delivery(self):
        intermediate={'name':'unusual-name.mp4','type':'video/mp4','source_step':'assemble','quality_state':'approved'}
        pending={**intermediate,'name':'candidate.mp4','quality_state':'pending'}
        result=order_artifacts([intermediate,pending]);self.assertTrue(all(x['is_process'] and not x['is_primary'] for x in result))
        delivered={**intermediate,'workflow_delivery':True,'role':'deliverable'}
        result=order_artifacts([pending,delivered]);self.assertEqual(result[0]['name'],'unusual-name.mp4');self.assertTrue(result[0]['is_primary'])
