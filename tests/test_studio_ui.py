import json
import io
import shutil
import subprocess
import tempfile
import time
import unittest
import zipfile
from pathlib import Path
from tests.isolation import temporary_root as isolated_tmp_root

from fastapi.testclient import TestClient

from webapp import agent_store
from webapp.agent_activity import ActivityTracker
from webapp.app import create_app


class StudioTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        root=isolated_tmp_root();root.mkdir(parents=True,exist_ok=True)
        cls.temp=tempfile.TemporaryDirectory(prefix='studio-tests-',dir=root)
        cls.root=Path(cls.temp.name);cls.clip=cls.root/'sample.mp4'
        subprocess.run(['ffmpeg','-v','error','-y','-f','lavfi','-i','testsrc2=s=320x180:r=24',
                        '-t','1','-c:v','libx264','-threads','1',str(cls.clip)],check=True)
        cls.audio=cls.root/'music.wav'
        subprocess.run(['ffmpeg','-v','error','-y','-f','lavfi','-i','sine=frequency=440:duration=1',
                        str(cls.audio)],check=True)

    @classmethod
    def tearDownClass(cls):cls.temp.cleanup()

    def setUp(self):
        self.directory=tempfile.TemporaryDirectory(dir=self.root)
        self.app=create_app(Path(self.directory.name),run_worker=False)
        self.a=TestClient(self.app);self.b=TestClient(self.app)
        self.a.__enter__();self.b.__enter__()
        self.pid=self.a.post('/api/projects',json={'name':'编辑项目'}).json()['id']
        self.other=self.b.post('/api/projects',json={'name':'Other project'}).json()['id']
        r=self.a.post(f'/api/projects/{self.pid}/assets',files={'file':('sample.mp4',self.clip.read_bytes(),'video/mp4')})
        self.assertEqual(r.status_code,201,r.text);self.asset=r.json()['id']
        (Path(self.directory.name)/'agent-health.json').write_text(json.dumps({'ready':True,'updated':time.time()}))

    def tearDown(self):
        self.b.__exit__(None,None,None);self.a.__exit__(None,None,None);self.directory.cleanup()

    def test_three_real_pages_and_private_editor_routes(self):
        home=self.a.get('/');self.assertEqual(home.status_code,200)
        self.assertIn('static/home.js',home.text);self.assertNotIn('id="agent-input"',home.text)
        for route,script in [('tools','tools.js'),('chat','dynamic.js')]:
            response=self.a.get(f'/{route}/{self.pid}')
            self.assertEqual(response.status_code,200)
            self.assertIn('<base href="/">',response.text)
            self.assertIn('static/'+script,response.text)
            self.assertNotIn('{{PROJECT}}',response.text)
            self.assertIn(self.pid,response.text)
            self.assertEqual(self.b.get(f'/{route}/{self.pid}').status_code,404)
            if route=='chat':
                self.assertIn('id="dynamic-preview"',response.text)
                self.assertIn('id="dag-fab"',response.text)
                self.assertIn('id="dag-dialog"',response.text)
                self.assertIn('id="dag-canvas"',response.text)
                self.assertIn('id="dynamic-input-history"',response.text)
                self.assertIn('.pdf,.docx,.xlsx,.pptx',response.text)

    def test_reference_documents_upload_with_structural_validation(self):
        text='镜头要求：保留人物动作，并参考品牌色。'.encode()
        uploaded=self.a.post(f'/api/projects/{self.pid}/assets',files={'file':('brief.md',text,'text/markdown')})
        self.assertEqual(uploaded.status_code,201,uploaded.text);self.assertEqual(uploaded.json()['kind'],'document')
        pdf=b'%PDF-1.4\n1 0 obj <<>> endobj\n%%EOF\n'
        response=self.a.post(f'/api/projects/{self.pid}/assets',files={'file':('reference.pdf',pdf,'application/pdf')})
        self.assertEqual(response.status_code,201,response.text);self.assertEqual(response.json()['mime'],'application/pdf')
        office=io.BytesIO()
        with zipfile.ZipFile(office,'w') as archive:
            archive.writestr('[Content_Types].xml','<Types/>');archive.writestr('word/document.xml','<document/>')
        response=self.a.post(f'/api/projects/{self.pid}/assets',files={'file':('notes.docx',office.getvalue(),'application/vnd.openxmlformats-officedocument.wordprocessingml.document')})
        self.assertEqual(response.status_code,201,response.text);self.assertEqual(response.json()['kind'],'document')
        detail=self.a.get(f'/api/projects/{self.pid}').json();documents=[item for item in detail['assets'] if item['kind']=='document']
        self.assertEqual({item['name'] for item in documents},{'brief.md','reference.pdf','notes.docx'})
        markdown=next(item for item in documents if item['name']=='brief.md')
        self.assertEqual(self.a.get('/'+markdown['url']).content,text)
        self.assertEqual(self.b.get('/'+markdown['url']).status_code,404)
        self.assertEqual(self.a.post(f'/api/projects/{self.pid}/assets',files={'file':('bad.txt',b'a\0b','text/plain')}).status_code,422)
        self.assertEqual(self.a.post(f'/api/projects/{self.pid}/assets',files={'file':('program.exe',b'MZ','application/octet-stream')}).status_code,415)

    def test_audio_upload_attachment_preview_and_ownership(self):
        upload=self.a.post(f'/api/projects/{self.pid}/assets',files={'file':('music.wav',self.audio.read_bytes(),'audio/wav')})
        self.assertEqual(upload.status_code,201,upload.text)
        audio=upload.json();self.assertEqual(audio['kind'],'audio')
        self.assertAlmostEqual(audio['duration'],1,delta=.03)
        assets=self.a.get(f'/api/projects/{self.pid}').json()['assets']
        item=next(a for a in assets if a['id']==audio['id'])
        self.assertIsNone(item['poster_url'])
        self.assertEqual(self.a.get('/'+item['url']).content,self.audio.read_bytes())
        self.assertEqual(self.b.get('/'+item['url']).status_code,404)
        turn=self.a.post(f'/api/projects/{self.pid}/agent/messages',json={'message':'用这段音乐配乐，先给方案','asset_ids':[audio['id']]})
        self.assertEqual(turn.status_code,202,turn.text)
        self.assertTrue(turn.json()['attachments'][0]['type'].startswith('audio/'))
        denied=self.b.post(f'/api/projects/{self.other}/agent/messages',json={'message':'use foreign audio','asset_ids':[audio['id']]})
        self.assertEqual(denied.status_code,422)

    def test_local_demo_plan_is_rebound_to_each_private_project(self):
        root=Path(self.directory.name)/'demos';(root/'sources').mkdir(parents=True)
        (root/'local-test').mkdir()
        shutil.copyfile(self.clip,root/'sources/demo.mp4')
        shutil.copyfile(self.audio,root/'sources/music.wav')
        plan={'version':1,'sources':{'v':'sources/demo.mp4','m':'sources/music.wav'},
              'clips':[{'source':'v','end':.8}],'audio':{'source':'m'}}
        (root/'local-test/plan.json').write_text(json.dumps(plan))
        (root/'catalog.json').write_text(json.dumps([{'id':'local-test','title':'Test',
            'renderer':'edit_media','workflow':'agent','prompt':'Add this music',
            'sources':[{'key':'v','file':'demo.mp4','name':'Demo'},{'key':'m','file':'music.wav','name':'Music'}],
            'preset':{'kind':'agent','assets':['v','m']}}]))
        source=self.a.get('/api/demos/local-test/sources/v');self.assertEqual(source.status_code,200);self.assertEqual(source.content,self.clip.read_bytes())
        self.assertEqual(self.a.get('/api/demos/local-test/sources/unknown').status_code,404)
        self.assertEqual(self.a.get('/api/demos/local-test/sources/..%2Fsecret').status_code,404)
        first=self.a.post('/api/demos/local-test/load');second=self.b.post('/api/demos/local-test/load')
        self.assertEqual(first.status_code,201,first.text);self.assertEqual(second.status_code,201,second.text)
        source_sets=[]
        for response in (first,second):
            project_id=response.json()['project']['id']
            directory=self.app.state.store.project_dir(project_id)
            bound=json.loads((directory/'agent/workspace/edit/demo-plan.json').read_text())
            source_sets.append(set(bound['sources'].values()))
            for relative in bound['sources'].values():self.assertTrue((directory/relative).is_file())
            self.assertEqual(bound['audio']['source'],'m')
        self.assertFalse(source_sets[0]&source_sets[1])

    def test_editor_base_path_works_behind_nginx_prefix(self):
        with TestClient(self.app,root_path='/video-use') as client:
            result=client.post('/video-use/api/projects',json={'name':'Prefix project'})
            self.assertEqual(result.status_code,201,result.text)
            page=client.get('/video-use/chat/'+result.json()['id'])
            self.assertEqual(page.status_code,200,page.text)
            self.assertIn('<base href="/video-use/">',page.text)

    def test_draft_survives_navigation_and_rejects_foreign_assets(self):
        preset={'kind':'render','segments':[{'asset':self.asset,'start':0,'end':.8}],
                'grade':'warm_cinematic','quality':'preview'}
        payload={'preset':preset,'prompt':'保留自然节奏','editor':'tools'}
        self.assertEqual(self.a.put(f'/api/projects/{self.pid}/draft',json=payload).status_code,200)
        detail=self.a.get(f'/api/projects/{self.pid}').json()
        self.assertEqual(detail['draft']['preset']['grade'],'warm_cinematic')
        self.assertEqual(detail['draft']['preset']['segments'][0]['end'],.8)
        self.assertEqual(detail['draft']['prompt'],'保留自然节奏')
        self.assertEqual(self.b.put(f'/api/projects/{self.pid}/draft',json=payload).status_code,404)
        self.assertEqual(self.b.put(f'/api/projects/{self.other}/draft',json=payload).status_code,422)

    def test_source_and_generated_thumbnails_require_ownership(self):
        path=f'/api/projects/{self.pid}/assets/{self.asset}/thumbnail'
        r=self.a.get(path);self.assertEqual(r.status_code,200,r.text[:100]);self.assertTrue(r.content.startswith(b'\xff\xd8'))
        self.assertEqual(self.b.get(path).status_code,404)
        store=self.app.state.store;job=store.add_job(self.pid,'render',{})
        out=store.project_dir(self.pid)/'edit/jobs'/job['id'];out.mkdir(parents=True)
        shutil.copyfile(self.clip,out/'final.mp4');store.finish(job['id'],'succeeded',['final.mp4'])
        artifact=self.a.get(f'/api/projects/{self.pid}').json()['jobs'][0]['artifacts'][0]
        self.assertTrue(self.a.get('/'+artifact['poster_url']).content.startswith(b'\xff\xd8'))
        self.assertEqual(self.b.get('/'+artifact['poster_url']).status_code,404)
        tid=agent_store.add_turn(store,self.pid,'execute','test')
        exported=store.project_dir(self.pid)/'edit/agent_exports'/tid;exported.mkdir(parents=True)
        shutil.copyfile(self.clip,exported/'result.mp4')
        a={'name':'final.mp4','file':'result.mp4','url':f'api/projects/{self.pid}/agent/{tid}/files/result.mp4','type':'video/mp4'}
        agent_store.finish(store,tid,'succeeded',artifacts=[a])
        poster=self.a.get(f'/api/projects/{self.pid}/agent').json()['turns'][0]['artifacts'][0]['poster_url']
        self.assertEqual(self.a.get('/'+poster).status_code,200)
        self.assertEqual(self.b.get('/'+poster).status_code,404)

    def test_chat_attachment_snapshots_cannot_reference_another_project(self):
        payload={'message':'看看这个素材','asset_ids':[self.asset]}
        self.assertEqual(self.b.post(f'/api/projects/{self.other}/agent/messages',json=payload).status_code,422)
        response=self.a.post(f'/api/projects/{self.pid}/agent/messages',json=payload)
        self.assertEqual(response.status_code,202,response.text)
        attachment=response.json()['attachments'][0]
        self.assertEqual(attachment['id'],self.asset)
        self.assertEqual(attachment['name'],'sample.mp4')
        self.assertIn('/thumbnail',attachment['poster_url'])

    def test_event_stream_replays_and_resumes_without_leaking_other_users(self):
        store=self.app.state.store;tid=agent_store.add_turn(store,self.pid,'plan','test stream')
        agent_store.event(store,tid,'tool',{'id':'one','title':'读取素材','status':'running'})
        first=store.query('SELECT seq FROM agent_events WHERE run_id=?',(tid,))[0]['seq']
        agent_store.event(store,tid,'tool',{'id':'one','title':'读取素材','status':'completed'})
        agent_store.finish(store,tid,'succeeded',text='完成计划')
        path=f'/api/projects/{self.pid}/agent/{tid}/stream'
        response=self.a.get(path)
        self.assertEqual(response.status_code,200)
        self.assertIn('text/event-stream',response.headers['content-type'])
        self.assertIn('event: turn',response.text);self.assertIn('event: activity',response.text)
        self.assertIn('event: done',response.text);self.assertIn('完成计划',response.text)
        resumed=self.a.get(path,headers={'Last-Event-ID':str(first)})
        self.assertNotIn(f'id: {first}\n',resumed.text)
        self.assertEqual(resumed.text.count('event: activity'),1)
        self.assertEqual(self.b.get(path).status_code,404)


    def test_redis_terminal_snapshot_flushes_body_and_recovers_database_fallback(self):
        from unittest.mock import patch,AsyncMock
        from types import SimpleNamespace
        store=self.app.state.store;task=agent_store.add_turn(store,self.pid,'plan','stream')
        store.execute("UPDATE agent_turns SET status='running',text='我' WHERE id=?",(task,))
        count=0
        def read(*args):
            nonlocal count
            count+=1
            if count==1:return None
            if count==2:
                agent_store.finish(store,task,'succeeded','完整说明已经结束')
                return [{'id':'999-0','kind':'terminal','payload':{'status':'succeeded'}}]
            return []
        fake=SimpleNamespace(url='redis://fixture',enabled=True,read=AsyncMock(side_effect=read),aclose=AsyncMock())
        with patch('webapp.agent_api.agent_stream_bus',return_value=fake),patch('webapp.agent_stream.AsyncStreamReader',return_value=fake):
            result=self.a.get(f'/api/projects/{self.pid}/agent/{task}/stream')
        fake.aclose.assert_awaited_once()
        self.assertEqual(result.status_code,200)
        turns=[json.loads(frame.split('data: ',1)[1]) for frame in result.text.split('\n\n') if 'event: turn\n' in frame]
        self.assertTrue(any(t.get('stream_transport')=='database' for t in turns))
        self.assertEqual(turns[-1]['text'],'完整说明已经结束');self.assertFalse(turns[-1].get('_text_unchanged',False))
        self.assertEqual(turns[-1]['status'],'succeeded')


class ActivityTests(unittest.TestCase):
    def test_actual_tool_lifecycle_output_and_failure_are_preserved(self):
        events=[];tracker=ActivityTracker(lambda kind,payload:events.append((kind,payload)),
            [{'filename':'input.mp4','name':'我的素材.mp4'}])
        item={'id':'cmd1','type':'commandExecution','command':'ffmpeg -i input.mp4 -c:v libx264 final.mp4'}
        tracker.handle('item/started',{'item':item,'startedAtMs':1000})
        tracker.handle('item/commandExecution/outputDelta',{'itemId':'cmd1','delta':'frame=24 time=00:00:01.0'})
        tracker.handle('item/completed',{'item':dict(item,exitCode=1,aggregatedOutput='encoder failed'),'completedAtMs':2500})
        self.assertEqual(events[0][1]['category'],'render')
        self.assertIn('我的素材.mp4',events[0][1]['detail'])
        self.assertEqual(events[1][0],'tool_output')
        self.assertEqual(events[-1][1]['status'],'failed')
        self.assertEqual(events[-1][1]['output'],'encoder failed')
        self.assertEqual(events[-1][1]['started'],1)
        self.assertEqual(events[-1][1]['completed'],2.5)

    def test_public_plan_and_notes_are_shown_but_reasoning_is_not(self):
        events=[];tracker=ActivityTracker(lambda kind,payload:events.append((kind,payload)))
        tracker.handle('item/completed',{'item':{'id':'reasoning','type':'reasoning','summary':['private reasoning']}})
        self.assertEqual(events,[])
        tracker.handle('turn/plan/updated',{'plan':[{'step':'检查素材','status':'completed'}]})
        tracker.handle('item/completed',{'item':{'id':'note','type':'agentMessage','phase':'commentary','text':'正在查看画面'}})
        self.assertEqual([kind for kind,_ in events],['plan','note'])


if __name__=='__main__':unittest.main()
