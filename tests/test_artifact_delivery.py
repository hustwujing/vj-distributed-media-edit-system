import json
import os
import tempfile
import unittest
from pathlib import Path
from tests.isolation import temporary_root as isolated_tmp_root

from webapp.artifacts import order_artifacts
from webapp.agent_store import public
from webapp.agent_sandbox import export_results,snapshot_outputs


def video(name):return {'name':name,'file':name,'type':'video/mp4','size':3,'url':'api/files/'+name}


class DeliveryOrderTests(unittest.TestCase):
    def test_existing_fifth_video_is_the_primary_delivery(self):
        files=[video('final.mp4'),*[video(f'final-v{i}.mp4') for i in range(2,6)]]
        row={'id':'t','project':'p','mode':'execute','prompt':'execute','status':'succeeded','text':'已完成，最新成片为 **final-v5.mp4**。',
             'error':'','created':1,'updated':2,'artifacts':files}
        result=public(row)['artifacts']
        self.assertEqual(result[0]['name'],'final-v5.mp4')
        self.assertTrue(result[0]['is_primary'])
        self.assertEqual([a['name'] for a in result if not a['alternate_version']],['final-v5.mp4'])
        self.assertEqual(len(result),5)
        self.assertEqual(files[0]['name'],'final.mp4')

    def test_workflow_build_documents_are_intermediate_until_declared_delivery(self):
        row={'id':'t','project':'p','mode':'execute','prompt':'execute','status':'paused','text':'Needs review',
             'error':'','created':1,'updated':2,'workflow':{'status':'paused'},
             'artifacts':[{'name':'arbitrary-construction.json','type':'application/json','is_primary':True},
                          {**video('candidate.mp4'),'quality_state':'pending'}]}
        result=public(row)['artifacts']
        self.assertTrue(all(x['is_process'] and not x['is_primary'] for x in result))
        row['status']='succeeded';row['workflow']['status']='succeeded';row['artifacts'][0]['workflow_delivery']=True
        result=public(row)['artifacts'];self.assertEqual(result[0]['name'],'arbitrary-construction.json');self.assertTrue(result[0]['is_primary'])

    def test_numeric_versions_and_explicit_rollback(self):
        files=[video('final-v9.mp4'),video('final.mp4'),video('final-v10.mp4')]
        self.assertEqual(order_artifacts(files)[0]['name'],'final-v10.mp4')
        self.assertEqual(order_artifacts(files,primary='final-v9.mp4')[0]['name'],'final-v9.mp4')
        self.assertEqual(order_artifacts(files,'本轮交付 final-v9.mp4。')[0]['name'],'final-v9.mp4')

    def test_declared_subtitle_primary_survives_serialization_with_manifest(self):
        files=[{'name':'workflow/deliverables.json','type':'application/json','role':'deliverable','source_modified':3},
               {'name':'workflow/speech.srt','type':'text/plain','role':'deliverable','is_primary':True,'source_modified':1},
               {'name':'workflow/speech.txt','type':'text/plain','role':'deliverable','source_modified':2}]
        ordered=order_artifacts(files)
        self.assertEqual(ordered[0]['name'],'workflow/speech.srt')
        self.assertTrue(ordered[0]['is_primary'])
        self.assertEqual(order_artifacts(ordered)[0]['name'],'workflow/speech.srt')

    def test_process_video_never_becomes_the_primary(self):
        self.assertEqual(order_artifacts([video('base.mp4'),video('portrait.mp4')])[0]['name'],'portrait.mp4')
        declared=[{**video('base.mp4'),'role':'deliverable'},video('portrait.mp4')]
        self.assertEqual(order_artifacts(declared,primary='base.mp4')[0]['name'],'base.mp4')


class ChangedOutputTests(unittest.TestCase):
    def environment(self,root):
        work=root/'workspace';(work/'edit').mkdir(parents=True)
        return {'workspace':work,'project':root/'project','uid':os.getuid(),'gid':os.getgid()}

    def test_only_changed_outputs_are_published_and_hashed(self):
        with tempfile.TemporaryDirectory(dir=isolated_tmp_root()) as directory:
            env=self.environment(Path(directory));edit=env['workspace']/'edit'
            (edit/'final.mp4').write_bytes(b'old');(edit/'final-v4.mp4').write_bytes(b'old4')
            env['output_baseline']=snapshot_outputs(env)
            (edit/'final-v5.mp4').write_bytes(b'new5');(edit/'project.md').write_text('new decision')
            artifacts=export_results(env,'turn','最新成片 final-v5.mp4')
            self.assertEqual([a['name'] for a in artifacts],['final-v5.mp4','project.md'])
            self.assertEqual(len(artifacts[0]['sha256']),64)
            self.assertTrue(artifacts[0]['is_primary'])
            self.assertEqual((edit/'final.mp4').read_bytes(),b'old')

    def test_explicit_redelivery_and_stale_manifest(self):
        with tempfile.TemporaryDirectory(dir=isolated_tmp_root()) as directory:
            env=self.environment(Path(directory));edit=env['workspace']/'edit'
            (edit/'final-v4.mp4').write_bytes(b'four')
            (edit/'deliverables.json').write_text(json.dumps({'primary':'final-v4.mp4','files':['final-v4.mp4']}))
            env['output_baseline']=snapshot_outputs(env)
            (edit/'final-v5.mp4').write_bytes(b'five')
            artifacts=export_results(env,'fresh')
            self.assertEqual([a['name'] for a in artifacts],['final-v5.mp4'])
            env['output_baseline']=snapshot_outputs(env)
            (edit/'deliverables.json').write_text(json.dumps({'primary':'final-v4.mp4','files':['final-v4.mp4'],'reason':'restore'}))
            artifacts=export_results(env,'restore')
            self.assertEqual(artifacts[0]['name'],'final-v4.mp4')
            self.assertTrue(artifacts[0]['is_primary'])

    def test_primary_is_exported_before_artifact_limit(self):
        with tempfile.TemporaryDirectory(dir=isolated_tmp_root()) as directory:
            env=self.environment(Path(directory));edit=env['workspace']/'edit'
            for i in range(50):(edit/f'check_{i:02}.txt').write_text('check')
            (edit/'final-v5.mp4').write_bytes(b'video')
            artifacts=export_results(env,'limit')
            self.assertEqual(len(artifacts),40)
            self.assertEqual(artifacts[0]['name'],'final-v5.mp4')

    def test_invalid_changed_manifest_preserves_files_without_claiming_delivery(self):
        invalid=[{'primary':'edit/result.txt','files':[{'path':'edit/result.txt'}]},
                 {'primary':'missing.txt','files':['result.txt']},
                 {'primary':'result.txt','files':['../secret.txt']},
                 {'primary':'result.txt','files':['result.txt','missing.txt']},
                 '{broken-json']
        for declaration in invalid:
            with self.subTest(declaration=declaration),tempfile.TemporaryDirectory(dir=isolated_tmp_root()) as directory:
                env=self.environment(Path(directory));edit=env['workspace']/'edit';env['output_baseline']=snapshot_outputs(env)
                (edit/'result.txt').write_text('Actual generated content')
                (edit/'deliverables.json').write_text(declaration if isinstance(declaration,str) else json.dumps(declaration))
                artifacts=export_results(env,'invalid','已完成，交付 result.txt')
                self.assertTrue(env.get('delivery_error'))
                candidate=next(a for a in artifacts if a['name']=='result.txt')
                self.assertEqual(candidate['quality_state'],'pending');self.assertFalse(candidate['is_primary'])
                self.assertTrue(candidate['is_process'])
                self.assertEqual((env['project']/'edit/agent_exports/invalid'/candidate['file']).read_text(),'Actual generated content')
                self.assertTrue((edit/'deliverables.json').exists())

    def test_old_invalid_manifest_does_not_poison_new_output_or_explicit_repair(self):
        with tempfile.TemporaryDirectory(dir=isolated_tmp_root()) as directory:
            env=self.environment(Path(directory));edit=env['workspace']/'edit'
            (edit/'deliverables.json').write_text('{bad');env['output_baseline']=snapshot_outputs(env)
            (edit/'result.txt').write_text('content')
            artifacts=export_results(env,'new');self.assertFalse(env.get('delivery_error'))
            self.assertTrue(artifacts[0]['is_primary'])
            env['output_baseline']=snapshot_outputs(env)
            (edit/'deliverables.json').write_text(json.dumps({'primary':'result.txt','files':['result.txt']}))
            artifacts=export_results(env,'repair');self.assertEqual(artifacts[0]['name'],'result.txt')
            self.assertEqual(artifacts[0]['role'],'deliverable');self.assertTrue(artifacts[0]['is_primary'])

    def test_every_declared_delivery_precedes_incidental_files_at_export_limit(self):
        with tempfile.TemporaryDirectory(dir=isolated_tmp_root()) as directory:
            env=self.environment(Path(directory));edit=env['workspace']/'edit'
            for number in range(45):(edit/f'preview-{number}.png').write_bytes(b'preview')
            (edit/'result.mp4').write_bytes(b'video');(edit/'required-report.txt').write_text('report')
            (edit/'deliverables.json').write_text(json.dumps({'primary':'result.mp4','files':['result.mp4','required-report.txt']}))
            artifacts=export_results(env,'many')
            self.assertEqual(len(artifacts),40)
            self.assertEqual({a['name'] for a in artifacts if a['role']=='deliverable'},{'result.mp4','required-report.txt'})

    def test_declared_file_becoming_unreadable_cannot_silently_complete(self):
        from unittest.mock import patch
        with tempfile.TemporaryDirectory(dir=isolated_tmp_root()) as directory:
            env=self.environment(Path(directory));edit=env['workspace']/'edit'
            target=edit/'result.txt';target.write_text('generated')
            (edit/'deliverables.json').write_text(json.dumps({'primary':'result.txt','files':['result.txt']}))
            original=os.open
            def changed(path,*args,**kwargs):
                if path==target:raise FileNotFoundError('source disappeared after validation')
                return original(path,*args,**kwargs)
            with patch('webapp.agent_sandbox.os.open',side_effect=changed):artifacts=export_results(env,'missing')
            self.assertTrue(env.get('delivery_error'))
            self.assertTrue(all(not a['is_primary'] and a['is_process'] for a in artifacts))


if __name__=='__main__':unittest.main()
