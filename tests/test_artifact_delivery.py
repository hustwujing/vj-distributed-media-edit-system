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


if __name__=='__main__':unittest.main()
