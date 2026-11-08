import unittest
from webapp.operations import get,Operation
from webapp.operation_result import validate_contract
from webapp.workflow_executor import bind_output_roles


class RequiredOperationOutputTests(unittest.TestCase):
    def validate(self,identifier,records):
        step={'spec':{'kind':'operation','operation':identifier}}
        files,roles=bind_output_roles(step,[record['name'] for record in records])
        report={'technical_valid':True,'passed':True,'checks':[],'files':records}
        return validate_contract(get(identifier),report,roles)

    def test_audio_missing_report_fails_before_final_review(self):
        for ext in ('wav','m4a','mp3'):
            with self.subTest(ext=ext):
                report=self.validate('audio.process',[{'name':'edit/final.'+ext,'kind':'media','audio_track_present':True}])
                self.assertFalse(report['technical_valid'])
                self.assertFalse(report['passed'])
                self.assertTrue(any(row.get('code')=='output_role_missing' and row.get('role')=='report' for row in report['checks']))

    def test_complete_audio_result_passes_but_wrong_report_type_fails(self):
        audio={'name':'edit/final.wav','kind':'media','audio_track_present':True}
        report={'name':'edit/audio-process-report.json','kind':'json'}
        self.assertTrue(self.validate('audio.process',[audio,report])['technical_valid'])
        report['kind']='image'
        self.assertFalse(self.validate('audio.process',[audio,report])['technical_valid'])

    def test_fixed_evidence_names_bind_and_missing_preview_fails(self):
        report={'name':'edit/evidence-report.json','kind':'json'}
        self.assertFalse(self.validate('media.evidence',[report])['technical_valid'])
        self.assertTrue(self.validate('media.evidence',[report,{'name':'edit/preview.jpg','kind':'image'}])['technical_valid'])

    def test_optional_and_alternative_outputs_remain_valid(self):
        self.assertTrue(self.validate('media.render',[{'name':'edit/final.mp4','kind':'media','video':True}])['technical_valid'])
        for record in ({'name':'edit/final.wav','kind':'media','audio_track_present':True},
                       {'name':'edit/final.mp4','kind':'media','video':True}):
            self.assertTrue(self.validate('media.edit',[record])['technical_valid'])

    def test_catalog_exposes_mandatory_roles_and_rejects_invalid_registration(self):
        self.assertEqual(get('audio.process').public()['output_contract']['required_roles'],['audio','report'])
        with self.assertRaisesRegex(ValueError,'Required output roles'):
            Operation('test.invalid','test','fixed_media',required_output_roles=('missing',))
