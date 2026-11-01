import contextlib
import io
import json
import subprocess
import tempfile
import unittest
from unittest.mock import patch
from pathlib import Path
from tests.isolation import temporary_root as isolated_tmp_root

from fastapi.testclient import TestClient
from webapp.app import create_app
from webapp.worker import execute
from webapp.quality_review import _events


class ManualToolTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory(prefix='manual-tools-',dir=isolated_tmp_root())
        self.root=Path(self.temp.name);self.app=create_app(self.root,run_worker=False)
        self.client=TestClient(self.app);self.client.__enter__()
        self.pid=self.client.post('/api/projects',json={'name':'参数工具测试'}).json()['id']
        self.clip=self.root/'test.mp4'
        subprocess.run(['ffmpeg','-v','error','-y','-f','lavfi','-i','testsrc2=s=160x120:r=20:d=2',
                        '-c:v','libx264','-threads','1',str(self.clip)],check=True)
        r=self.client.post(f'/api/projects/{self.pid}/assets',files={'file':('test.mp4',self.clip.read_bytes(),'video/mp4')})
        self.asset=r.json()['id']

    def tearDown(self):self.client.__exit__(None,None,None);self.temp.cleanup()

    def payload(self):
        return {'kind':'edit','tool':'speed','edit_plan':{'version':1,'clips':[{'asset':self.asset,'start':0,'end':1,'speed':2}],
                    'output':{'width':160,'height':120,'fps':20}}}

    def test_parameters_are_bound_and_rendered_through_shared_helper(self):
        r=self.client.post(f'/api/projects/{self.pid}/jobs',json=self.payload())
        self.assertEqual(r.status_code,202,r.text);self.assertEqual(r.json()['tool'],'speed')
        job=self.app.state.store.job(r.json()['id']);project=self.app.state.store.project_dir(self.pid)
        out=project/'edit/jobs'/job['id']
        with contextlib.redirect_stdout(io.StringIO()):
            files=execute({'project_dir':str(project),'output_dir':str(out),'payload':job['payload'],'assets':self.app.state.store.assets(self.pid)})
        self.assertIn('final.mp4',files)
        report=json.loads((out/'final.report.json').read_text())
        self.assertAlmostEqual(report['media']['duration'],.5,delta=.05)
        self.assertEqual(report['media']['width'],160)
        self.app.state.store.finish(job['id'],'succeeded',files)
        info=self.client.get(f'/api/projects/{self.pid}').json()['jobs'][0]
        video=next(a for a in info['artifacts'] if a['name']=='final.mp4')
        self.assertEqual(self.client.get('/'+video['url'],headers={'Range':'bytes=0-31'}).status_code,206)
        self.assertNotIn(str(self.root),(out/'edit_plan.json').read_text())

    def test_foreign_assets_paths_and_bad_parameters_are_rejected(self):
        foreign=self.client.post('/api/projects',json={'name':'Another project'}).json()['id']
        foreign_asset=self.client.post(f'/api/projects/{foreign}/assets',files={'file':('x.mp4',self.clip.read_bytes(),'video/mp4')}).json()['id']
        variants=[]
        p=self.payload();p['edit_plan']['clips'][0]['asset']=foreign_asset;variants.append(p)
        p=self.payload();p['edit_plan']['sources']={'a':'/etc/passwd'};variants.append(p)
        p=self.payload();p['edit_plan']['clips'][0]['source']='/etc/passwd';variants.append(p)
        p=self.payload();p['edit_plan']['subtitles']={'file':'/etc/passwd'};variants.append(p)
        p=self.payload();p['edit_plan']['clips'][0]['speed']=20;variants.append(p)
        p=self.payload();p['edit_plan']['clips'][0]['end']=10;variants.append(p)
        p=self.payload();p['edit_plan']['unknown_filter']='movie=/etc/passwd';variants.append(p)
        for p in variants:
            with self.subTest(payload=p):self.assertEqual(self.client.post(f'/api/projects/{self.pid}/jobs',json=p).status_code,422)
        self.assertEqual(self.app.state.store.query('SELECT id FROM jobs'),[])

    def test_current_tool_draft_persists_and_checks_asset_ownership(self):
        body={'preset':self.payload(),'prompt':'保留原声','editor':'tools'}
        self.assertEqual(self.client.put(f'/api/projects/{self.pid}/draft',json=body).status_code,200)
        saved=self.client.get(f'/api/projects/{self.pid}').json()['draft']
        self.assertEqual(saved['preset']['tool'],'speed')
        self.assertEqual(saved['preset']['edit_plan']['clips'][0]['speed'],2)
        other=self.client.post('/api/projects',json={'name':'Other'}).json()['id']
        self.assertEqual(self.client.put(f'/api/projects/{other}/draft',json=body).status_code,422)

    def test_review_is_private_and_reports_deterministic_media_signals(self):
        endpoint=f'/api/projects/{self.pid}/jobs'
        denied=self.client.post(endpoint,json={'kind':'review','assets':['foreign'],'start':0,'end':1})
        self.assertEqual(denied.status_code,422)
        queued=self.client.post(endpoint,json={'kind':'review','tool':'review','assets':[self.asset],'start':0,'end':1})
        self.assertEqual(queued.status_code,202,queued.text)
        job=self.app.state.store.job(queued.json()['id']);project=self.app.state.store.project_dir(self.pid);out=project/'edit/jobs'/job['id']
        with contextlib.redirect_stdout(io.StringIO()):
            files=execute({'project_dir':str(project),'output_dir':str(out),'payload':job['payload'],'assets':self.app.state.store.assets(self.pid)})
        self.assertEqual(files,['quality-review.json'])
        report=json.loads((out/'quality-review.json').read_text())
        self.assertFalse(report['model_required']);self.assertIn('signals',report);self.assertIn('black_frames',report['signals'])
        self.app.state.store.finish(job['id'],'succeeded',files)
        with TestClient(self.app) as other:
            self.assertEqual(other.get(f'/api/projects/{self.pid}/jobs/{job["id"]}/files/quality-review.json').status_code,404)

    def test_black_event_parser_uses_reported_end_and_duration(self):
        with patch('webapp.quality_review._run',return_value='black_start:1.2 black_end:2.5 black_duration:1.3'):
            self.assertEqual(_events(Path('x'),'black','x',()),[{'start':1.2,'end':2.5,'duration':1.3}])

    def test_audio_normalization_is_validated_and_rendered_locally(self):
        plan={'version':1,'clips':[{'asset':self.asset,'start':0,'end':1}],
              'output':{'audio_only':True,'format':'wav'},'audio':{'normalize':True,'denoise':False,'volume':1}}
        r=self.client.post(f'/api/projects/{self.pid}/jobs',json={'kind':'edit','tool':'audio','edit_plan':plan})
        self.assertEqual(r.status_code,202,r.text)
        job=self.app.state.store.job(r.json()['id']);project=self.app.state.store.project_dir(self.pid);out=project/'edit/jobs'/job['id']
        with contextlib.redirect_stdout(io.StringIO()):files=execute({'project_dir':str(project),'output_dir':str(out),'payload':job['payload'],'assets':self.app.state.store.assets(self.pid)})
        self.assertIn('final.wav',files);self.assertTrue((out/'final.report.json').is_file())

    def test_private_editorial_notes_branches_and_chapters(self):
        base=f'/api/projects/{self.pid}/editorial'
        draft={'preset':self.payload(),'prompt':'原计划','editor':'tools'};self.client.put(f'/api/projects/{self.pid}/draft',json=draft)
        note=self.client.post(base+'/notes',json={'start':.2,'end':.8,'text':'删掉停顿','kind':'remove'});self.assertEqual(note.status_code,201,note.text)
        branch=self.client.post(base+'/branches',json={'name':'竖版短视频'});self.assertEqual(branch.status_code,201)
        chapter=self.client.post(base+'/chapters',json={'asset':self.asset,'start':0,'end':1,'title':'开场'});self.assertEqual(chapter.status_code,201)
        data=self.client.get(base).json();self.assertEqual(data['notes'][0]['text'],'删掉停顿');self.assertEqual(data['branches'][0]['name'],'竖版短视频');self.assertEqual(data['chapters'][0]['title'],'开场')
        self.client.put(f'/api/projects/{self.pid}/draft',json={'preset':None,'prompt':'changed','editor':'chat'})
        self.assertEqual(self.client.post(base+'/branches/'+branch.json()['id']+'/activate').status_code,200)
        self.assertEqual(self.client.get(f'/api/projects/{self.pid}').json()['draft']['preset']['tool'],'speed')
        activated=self.client.post(base+'/chapters/'+chapter.json()['id']+'/activate');self.assertEqual(activated.status_code,200)
        self.assertEqual(activated.json()['preset']['segments'][0]['end'],1)
        saved=self.client.post(base+'/captions',json={'name':'精修','cues':[{'start':0,'end':.8,'text':'你好'},{'start':.8,'end':1.5,'text':'世界'}]})
        self.assertEqual(saved.status_code,201,saved.text);subtitle=saved.json()['id']
        cues=self.client.get(base+'/captions/'+subtitle).json()['cues'];self.assertEqual([c['text'] for c in cues],['你好','世界'])
        subtitle_asset=next(a for a in self.app.state.store.assets(self.pid) if a['id']==subtitle);self.assertEqual(subtitle_asset['metadata']['derived_from'],'caption_editor')
        template=self.client.post(base+'/templates',json={'name':'品牌横屏','width':1280,'height':720,'fit':'contain'});self.assertEqual(template.status_code,201,template.text)
        batch=self.client.post(base+'/batch',json={'template':template.json()['id'],'assets':[self.asset]});self.assertEqual(batch.status_code,202,batch.text)
        self.assertEqual(len(batch.json()['jobs']),1)
        with TestClient(self.app) as other:
            self.assertEqual(other.get(base).status_code,404);self.assertEqual(other.get(base+'/captions/'+subtitle).status_code,404);self.assertEqual(other.post(base+'/notes',json={'start':0,'text':'偷看'}).status_code,404)
        self.assertEqual(self.client.delete(base+'/notes/'+note.json()['id']).status_code,200)


if __name__=='__main__':unittest.main()
