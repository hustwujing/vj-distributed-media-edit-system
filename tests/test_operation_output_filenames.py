import unittest
from webapp.operations import get
from webapp.workflows import Step

class OperationOutputFilenameTests(unittest.TestCase):
    def test_delivery_candidates_use_real_output_roles_over_reports_and_verify_files(self):
        from webapp.workflow_agent_contracts import delivery_candidate_refs
        task={'accepted_result':['edit/final.mp4','edit/final.report.json','edit/final.verify_0.jpg'],
              'quality':{'output_roles':{'video':'edit/final.mp4','report':'edit/final.report.json'}}}
        self.assertEqual(delivery_candidate_refs(task),{'edit/final.mp4'})
        report={'accepted_result':['edit/report.json'],'quality':{'output_roles':{'report':'edit/report.json'}}}
        self.assertEqual(delivery_candidate_refs(report),{'edit/report.json'})
    def test_registered_output_role_is_bound_to_the_attempt_file_before_quality_check(self):
        from webapp.workflow_runtime import resolve_acceptance_output_roles
        rules=[{'kind':'image','file':'primary_image','width':512},{'kind':'file','file':'literal.png'}]
        resolved=resolve_acceptance_output_roles(rules,{'primary_image':'edit/run/output/image.png'})
        self.assertEqual(resolved[0]['file'],'edit/run/output/image.png')
        self.assertEqual(resolved[1]['file'],'literal.png')
        self.assertEqual(rules[0]['file'],'primary_image')

    def step(self,operation,name):
        return {'id':'operation','name':'operation','kind':'operation','operation':operation,'inputs':{'source':{'asset':'owned'}},
                'goal':'check result','on_failure':'diagnose','parameters':{},'acceptance':[{'kind':'json','file':name}]}
    def test_fixed_filename_mismatch_is_rejected_before_execution(self):
        for operation,bad,good in [('image.ocr','ocr-report.json','ocr.json'),('audio.transcribe','speech.json','transcript.json')]:
            with self.subTest(operation=operation):
                with self.assertRaisesRegex(ValueError,'输出契约'):Step.model_validate(self.step(operation,bad))
                parsed=Step.model_validate(self.step(operation,good))
                self.assertEqual(parsed.acceptance[0].file,good)
                self.assertIn(good,get(operation).public()['output_contract']['filenames'])
    def test_unrelated_dynamic_operations_are_not_given_guessed_filenames(self):
        self.assertIsNone(get('image.edit').output_files)
        self.assertIsNone(get('image.batch_export').output_files)
        self.assertEqual(get('image.remove_background').output_files,('foreground.png','mask.png'))

    def test_role_filename_mapping_excludes_diagnostic_attachments(self):
        contract=get('image.refine_zim').public()['output_contract']
        self.assertEqual(contract['role_filenames'],{'foreground':'foreground.png','mask':'mask.png'})
        self.assertIn('zim-preview.jpg',contract['filenames'])

    def test_script_media_acceptance_requires_a_declared_media_output(self):
        step={'id':'verify','name':'verify','kind':'script','argv':['python','edit/check.py'],
              'outputs':['report.json','frame.jpg'],'goal':'verify the video','on_failure':'diagnose',
              'acceptance':[{'kind':'media','min_duration':4.8},{'kind':'json','file':'report.json'}]}
        with self.assertRaisesRegex(ValueError,'media 验收没有对应类型'):Step.model_validate(step)
        Step.model_validate({**step,'outputs':[*step['outputs'],'final.MKV']})
        # Optional signals may intentionally have no matching output.
        Step.model_validate({**step,'acceptance':[{'kind':'media','severity':'signal','min_duration':4.8}]})
        Step.model_validate({**step,'acceptance':[{'kind':'json','file':'report.json'},{'kind':'image','file':'frame.jpg'}]})

    def test_known_operation_output_types_are_checked_without_guessing_dynamic_files(self):
        step=self.step('image.ocr','ocr.json');step['acceptance']=[{'kind':'media','min_duration':1}]
        with self.assertRaisesRegex(ValueError,'media 验收没有对应类型'):Step.model_validate(step)
        step['acceptance']=[{'kind':'image','file':'ocr-preview.png'}]
        Step.model_validate(step)
