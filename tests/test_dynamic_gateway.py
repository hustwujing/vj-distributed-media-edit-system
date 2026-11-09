import http.client
import json
import os
import socket
import unittest
from tests import test_dynamic_graph as fixtures
from webapp.agent_gateway import Gateway
from webapp import workflow_graph as graph
from webapp.task_leases import Coordinator,TaskQuotaReached


class DynamicGatewayTests(unittest.TestCase):
    def setUp(self):
        self.fixture=fixtures.DynamicGraphTests();self.fixture.setUp()
        self.addCleanup(self.fixture.tearDown)
        self.store=self.fixture.store;self.run=self.fixture.tick()
        self.task=next(t for t in graph.work_items(self.run) if t.get('control_kind'))
        self.lease=Coordinator(self.store,'gateway-protocol','test').claim(self.run['id'],'workflow',step_id=self.task['id'])
        self.assertIsNotNone(self.lease);self.addCleanup(self.lease.release)

    def bind_reply(self,reply):
        if not isinstance(reply,dict) or reply.get('action')!='proposal':return reply
        return {**reply,'proposal':fixtures.envelope(self.store,self.task['spec']['request_id'],reply['proposal'])}

    def test_still_jpeg_inspection_decodes_real_bytes_without_timestamp_seek(self):
        from PIL import Image
        from types import SimpleNamespace
        from unittest.mock import patch
        from webapp.workflow_agents import inspect_media
        path=self.store.root/'portrait.jpg';Image.new('RGB',(2048,512),'red').save(path)
        env={'assets':[{'id':'asset','filename':'portrait.jpg'}]}
        host=SimpleNamespace(store=self.store,workflow_runner=SimpleNamespace(adapter=SimpleNamespace(environment=lambda _:env)))
        import sys
        def command(env,gateway,argv):return [str(path) if item=='/workspace/sources/portrait.jpg' else sys.executable if item=='/opt/venv/bin/python' else item for item in argv]
        with patch.object(self.lease,'runtime',object()),patch('webapp.agent_sandbox.command',side_effect=command):
            result=inspect_media(host,self.lease,self.run,self.task,{'ref':'asset','path':path,'still_image':True,'offset_seconds':0})
        self.assertEqual(result['coverage'],'still_image')
        self.assertTrue(result['image'].startswith('data:image/jpeg;base64,'))
        import base64,io
        preview=Image.open(io.BytesIO(base64.b64decode(result['image'].split(',')[1])))
        self.assertEqual(preview.size,(1024,256),'General document views must not collapse to256px wide')

    def test_audio_inspection_measures_the_full_stream(self):
        from types import SimpleNamespace
        from unittest.mock import patch
        from webapp.workflow_agents import inspect_media
        import subprocess,sys
        path=self.store.root/'tone.wav'
        subprocess.run(['ffmpeg','-nostdin','-v','error','-y','-f','lavfi','-i','sine=frequency=440:duration=1',str(path)],check=True)
        env={'assets':[{'id':'asset','filename':'tone.wav'}]}
        host=SimpleNamespace(store=self.store,workflow_runner=SimpleNamespace(adapter=SimpleNamespace(environment=lambda _:env)))
        module_root=str((__import__('pathlib').Path(__file__).resolve().parents[1]/'webapp'))
        def command(env,gateway,argv):
            return [str(path) if item=='/workspace/sources/tone.wav' else sys.executable if item=='/opt/venv/bin/python'
                    else item.replace("'/opt'",repr(module_root)) if "sys.path.insert(0,'/opt')" in item else item for item in argv]
        with patch.object(self.lease,'runtime',object()),patch('webapp.agent_sandbox.command',side_effect=command):
            result=inspect_media(host,self.lease,self.run,self.task,
                {'ref':'asset','path':path,'offset_seconds':0,'window_seconds':1})
        self.assertEqual(result['coverage'],'technical_metadata_and_full_audio_measurement')
        self.assertFalse(result['audio_measurement']['audio_silent'])
        self.assertEqual(result['audio_measurement']['measurement_scope'],'full')
        self.assertFalse(result['requested_window_applied'])

    def test_static_svg_inspection_returns_real_rendered_pixels_and_structure(self):
        from PIL import Image
        from types import SimpleNamespace
        from unittest.mock import patch
        import base64,io,sys,hashlib
        from webapp.workflow_agents import inspect,inspect_media
        path=self.store.root/'vector.svg';path.write_text('<svg xmlns="http://www.w3.org/2000/svg" width="64" height="64"><g fill="red"><rect width="64" height="64"/></g></svg>')
        reference='edit/candidate/vector.svg';record={'sha256':hashlib.sha256(path.read_bytes()).hexdigest(),'size':path.stat().st_size,'location':path.name}
        evidence=inspect(SimpleNamespace(shared_files=False,root=self.store.root),self.run,{reference:{'record':record}},reference)
        host=SimpleNamespace(store=self.store,workflow_runner=SimpleNamespace(adapter=SimpleNamespace(environment=lambda _:{'assets':[]})))
        def command(env,gateway,argv):return [str(path) if item=='/workspace/'+reference else sys.executable if item=='/opt/venv/bin/python' else item for item in argv]
        with patch.object(self.lease,'runtime',object()),patch('webapp.agent_sandbox.command',side_effect=command):
            result=inspect_media(host,self.lease,self.run,self.task,evidence,remaining_images=1)
        self.assertEqual(result['coverage'],'svg_structure_and_rendered_image')
        self.assertEqual(result['svg_rendering']['source'],'same_declared_svg')
        pixels=Image.open(io.BytesIO(base64.b64decode(result['image'].split(',')[1]))).convert('RGB')
        red,green,blue=pixels.getpixel((pixels.width//2,pixels.height//2))
        self.assertGreater(red,240);self.assertLess(green,10);self.assertLess(blue,10)
        self.assertEqual(result['elements'][1]['attributes']['fill'],'red')
        path.write_text('<svg xmlns="http://www.w3.org/2000/svg" width="64" height="64"><rect width="64" height="64" opacity="0"/></svg>')
        record.update(sha256=hashlib.sha256(path.read_bytes()).hexdigest(),size=path.stat().st_size)
        evidence=inspect(SimpleNamespace(shared_files=False,root=self.store.root),self.run,{reference:{'record':record}},reference)
        with patch.object(self.lease,'runtime',object()),patch('webapp.agent_sandbox.command',side_effect=command):
            unavailable=inspect_media(host,self.lease,self.run,self.task,evidence,remaining_images=1)
        self.assertNotIn('image',unavailable)
        self.assertEqual(unavailable['rendering_unavailable'],'renderer_returned_fully_transparent')
        self.assertEqual(unavailable['elements'][1]['attributes']['opacity'],'0')

    def test_exhausted_image_budget_is_feedback_before_any_pixel_decoder(self):
        from PIL import Image
        from types import SimpleNamespace
        from unittest.mock import patch
        from webapp.workflow_agents import inspect_media,InspectionInputError
        path=self.store.root/'limited.jpg';Image.new('RGB',(32,32),'red').save(path)
        env={'assets':[{'id':'asset','filename':'limited.jpg'}]};commands=[]
        host=SimpleNamespace(store=self.store,workflow_runner=SimpleNamespace(adapter=SimpleNamespace(environment=lambda _:env)))
        def command(env,gateway,argv):
            commands.append(argv)
            return [str(path) if item=='/workspace/sources/limited.jpg' else item for item in argv]
        with patch.object(self.lease,'runtime',object()),patch('webapp.agent_sandbox.command',side_effect=command):
            with self.assertRaisesRegex(InspectionInputError,'image budget exhausted'):
                inspect_media(host,self.lease,self.run,self.task,{'ref':'asset','path':path,'still_image':True,'offset_seconds':0},remaining_images=0)
        self.assertEqual(len(commands),1);self.assertEqual(commands[0][0],'/usr/bin/ffprobe')

    def test_image_limit_feedback_preserves_previous_evidence_and_allows_bounded_reply(self):
        import threading
        from types import SimpleNamespace
        from unittest.mock import patch
        from webapp.workflow_agents import execute,InspectionInputError
        from webapp.specialist_profiles import get
        replies=[{'action':'inspect','ref':'asset','offset':offset} for offset in range(9)]+[{'action':'proposal','proposal':{
            'proposal_id':'after-image-limit','base_graph_version':0,'reason':'Use inspected evidence',
            'add_tasks':[fixtures.script()],'sealed':True}}]
        limits=[];conversations=[]
        def model(host,lease,run,profile,conversation,images,**kwargs):
            conversations.append(conversation);self.assertLessEqual(len(images),profile.max_images)
            return replies.pop(0)
        def inspect_media(host,lease,run,task,evidence,remaining_images=None):
            limits.append(remaining_images)
            if remaining_images<=0:raise InspectionInputError('Inspection image budget exhausted')
            return {'ref':'asset','media':True,'image':'bounded-image'}
        host=SimpleNamespace(store=self.store,stop=threading.Event())
        with self.lease.bind(),patch('webapp.workflow_agents.model',side_effect=lambda *a,**kw:self.bind_reply(model(*a,**kw))), \
             patch('webapp.workflow_agents.inspect',return_value={'ref':'asset','media':True}), \
             patch('webapp.workflow_agents.inspect_media',side_effect=inspect_media):
            execute(host,self.lease,self.run,self.task)
        self.assertEqual(limits,list(range(8,-1,-1)))
        self.assertIn('image budget exhausted',conversations[-1])
        self.assertEqual(len(self.store.query('SELECT * FROM workflow_inspections')),8)
        self.assertEqual(len(self.store.query('SELECT * FROM task_results')),1)
        self.assertEqual(get('media_planner').snapshot()['max_images'],8)
        self.assertEqual(get('media_planner').snapshot()['response_transport'],'buffered')
        self.assertEqual(get('visual_quality_reviewer').snapshot()['response_transport'],'buffered')
        self.assertEqual(get('editing_implementer').snapshot()['response_transport'],'stream')
        import hashlib
        diagnostic=json.loads(self.store.query('SELECT record FROM workflow_diagnostics')[0]['record'])
        self.assertEqual(diagnostic['reference_sha256'],hashlib.sha256(b'asset').hexdigest())
        self.assertEqual(diagnostic['code'],'inspection_input_rejected')

    def test_duplicate_ref_and_offset_reuses_existing_evidence_instead_of_reinspecting(self):
        import threading
        from types import SimpleNamespace
        from unittest.mock import patch
        from webapp.workflow_agents import execute
        proposal={'proposal_id':'deduplicated','base_graph_version':0,'reason':'Use existing evidence',
                  'add_tasks':[fixtures.script()],'sealed':True}
        replies=[{'action':'inspect','ref':'asset','offset':0},{'action':'inspect','ref':'asset','offset':0},
                 {'action':'proposal','proposal':proposal}]
        conversations=[]
        def model(*args,**kwargs):conversations.append(args[4]);return replies.pop(0)
        host=SimpleNamespace(store=self.store,stop=threading.Event())
        with self.lease.bind(),patch('webapp.workflow_agents.model',side_effect=lambda *a,**kw:self.bind_reply(model(*a,**kw))), \
             patch('webapp.workflow_agents.inspect',return_value={'ref':'asset','text':'evidence'}) as reader:
            execute(host,self.lease,self.run,self.task)
        self.assertEqual(reader.call_count,1)
        self.assertIn('already inspected',conversations[-1])

    def test_real_zip_inspection_in_agent_loop_does_not_call_media_probe(self):
        import threading,zipfile
        from types import SimpleNamespace
        from unittest.mock import patch
        from webapp.workflow_agents import execute
        path=self.store.root/'segments.zip'
        with zipfile.ZipFile(path,'w') as archive:archive.writestr('segment_01.mp4',b'opaque video bytes')
        record=self.store.artifacts.upload(self.fixture.f.project,path)
        reference='edit/segments.zip';allowed={reference:{'record':{'artifact':record['id'],'sha256':record['sha256'],'size':record['size'],'location':path.name}}}
        proposal={'proposal_id':'archive-inspected','base_graph_version':0,'reason':'Use bounded metadata',
                  'add_tasks':[fixtures.script()],'sealed':True}
        host=SimpleNamespace(store=self.store,stop=threading.Event())
        with self.lease.bind(),patch('webapp.workflow_agents.inputs',return_value=allowed), \
             patch('webapp.workflow_agents.inspect_media') as media, \
             patch('webapp.workflow_agents.model',side_effect=[{'action':'inspect','ref':reference},self.bind_reply({'action':'proposal','proposal':proposal})]) as model:
            execute(host,self.lease,self.run,self.task)
        media.assert_not_called();self.assertIn('archive_directory',model.call_args_list[1].args[4])
        evidence=json.loads(self.store.query('SELECT evidence_json FROM workflow_inspections')[0]['evidence_json'])
        self.assertFalse(evidence['contents_verified']);self.assertEqual(evidence['member_count'],1)
        self.assertEqual(len(self.store.query('SELECT * FROM task_results')),1)

    def test_media_failure_persists_reference_digest_without_raw_reference(self):
        import hashlib,threading
        from types import SimpleNamespace
        from unittest.mock import patch
        from webapp.workflow_agents import MediaInspectionError
        from webapp.workflow_executor import execute
        reference='edit/private-filename/video.mp4'
        host=SimpleNamespace(store=self.store,stop=threading.Event(),workflow_runner=SimpleNamespace(adapter=object()))
        with self.lease.bind(),patch('webapp.workflow_agents.execute',side_effect=MediaInspectionError('inspection_probe_failed',1,reference)):
            execute(host,self.lease)
        raw=self.store.query('SELECT record FROM workflow_diagnostics')[0]['record'];diagnostic=json.loads(raw)
        self.assertEqual(diagnostic['reference_sha256'],hashlib.sha256(reference.encode()).hexdigest())
        self.assertEqual(diagnostic['returncode'],1);self.assertNotIn(reference,raw)
        self.assertEqual(self.store.query('SELECT outcome FROM task_results')[0]['outcome'],'failed')

    def test_invented_proposal_asset_is_corrected_before_staging_or_approval(self):
        from types import SimpleNamespace
        from unittest.mock import patch
        import threading
        from webapp.workflow_agents import execute
        proposal={'proposal_id':'fix-reference','base_graph_version':0,'reason':'Use actual source',
                  'add_tasks':[fixtures.script(inputs={'source':{'asset':'asset'}})],'sealed':True}
        bad=json.loads(json.dumps(proposal));bad['add_tasks'][0]['inputs']['source']['asset']='invented-asset'
        replies=[{'action':'proposal','proposal':bad},{'action':'proposal','proposal':proposal}]
        host=SimpleNamespace(store=self.store,stop=threading.Event())
        with self.lease.bind(),patch('webapp.workflow_agents.model',side_effect=[self.bind_reply(reply) for reply in replies]) as model:
            execute(host,self.lease,self.run,self.task)
        self.assertEqual(model.call_count,2)
        self.assertIn('Input asset does not exist',model.call_args_list[1].args[4])
        stored=json.loads(self.store.query('SELECT proposal_json FROM planner_runs')[0]['proposal_json'])
        self.assertEqual(stored['proposal']['add_tasks'][0]['inputs']['source']['asset'],'asset')
        self.assertEqual(len(self.store.query('SELECT * FROM task_results')),1)
        self.assertFalse(self.store.query('SELECT id FROM workflow_decisions'))

    def test_transparent_pixels_are_composited_in_visual_evidence(self):
        from PIL import Image
        from types import SimpleNamespace
        from unittest.mock import patch
        import base64,io,sys
        from webapp.workflow_agents import inspect_media
        path=self.store.root/'transparent.png';Image.new('RGBA',(32,32),(255,0,0,0)).save(path)
        env={'assets':[{'id':'asset','filename':'transparent.png'}]}
        host=SimpleNamespace(store=self.store,workflow_runner=SimpleNamespace(adapter=SimpleNamespace(environment=lambda _:env)))
        def command(env,gateway,argv):return [str(path) if item=='/workspace/sources/transparent.png' else sys.executable if item=='/opt/venv/bin/python' else item for item in argv]
        with patch.object(self.lease,'runtime',object()),patch('webapp.agent_sandbox.command',side_effect=command):
            result=inspect_media(host,self.lease,self.run,self.task,{'ref':'asset','path':path,'still_image':True,'offset_seconds':0})
        pixels=Image.open(io.BytesIO(base64.b64decode(result['image'].split(',')[1]))).convert('RGB')
        self.assertEqual(result['preview_background'],'#b8bec8')
        self.assertTrue(all(abs(a-b)<4 for a,b in zip(pixels.getpixel((16,16)),(184,190,200))))

    def test_manager_http_cannot_call_media_tools_or_commit_task_success(self):
        path=self.store.root/'graph.sock'
        with self.lease.bind():
            gateway=Gateway(path,self.store.project_dir(self.run['project_id']),[], 'plan','unit','https://provider.invalid','unit-key',os.getuid(),os.getgid(),lambda *_:None,lambda *_:None)
        gateway.thread.start();self.addCleanup(gateway.close)
        def post(route,body):
            connection=http.client.HTTPConnection('localhost',timeout=10)
            connection.sock=socket.socket(socket.AF_UNIX);connection.sock.connect(str(path))
            try:
                connection.request('POST',route,json.dumps(body),{'Content-Type':'application/json'})
                response=connection.getresponse();return response.status,json.loads(response.read())
            finally:connection.close()
        self.assertEqual(post('/image-tools',{'action':'generate'})[0],403)
        self.assertEqual(post('/workflow/agent-result',{'result':{'files':{}}})[0],403)
        raw={'proposal_id':'manager-raw-script','base_graph_version':0,'reason':'Unsafe raw script',
             'add_tasks':[fixtures.script()],'sealed':True}
        self.assertEqual(post('/workflow/proposal',{'proposal':fixtures.envelope(self.store,self.task['spec']['request_id'],raw)})[0],422)
        agent={**fixtures.script(),'type':'agent','profile':'editing_implementer'};agent.pop('argv',None)
        proposal={'proposal_id':'manager-http','base_graph_version':0,'reason':'Authorized next task',
                  'add_tasks':[agent],'sealed':True}
        for _ in range(2):
            status,body=post('/workflow/proposal',{'proposal':fixtures.envelope(self.store,self.task['spec']['request_id'],proposal)})
            self.assertEqual(status,200,body);self.assertTrue(body['staged'])
        current=graph.get(self.store,self.run['id'])
        self.assertEqual(current['graph_version'],0)
        self.assertFalse([t for t in graph.work_items(current) if not t.get('control_kind')])
        self.assertFalse(self.store.query('SELECT id FROM task_results'))
        self.assertEqual(self.store.query('SELECT COUNT(*) AS n FROM planner_runs')[0]['n'],1)

    def test_token_reservations_distinguish_derived_usage_and_missing_receipts(self):
        from webapp.workflow_agents import model_budget
        from webapp.usage_protocol import Receipt
        self.run['authorization']['model_token_budget']=1024
        self.store.execute('UPDATE runs SET authorization_json=? WHERE id=?',(graph.encode(self.run['authorization']),self.run['id']))
        receipt=Receipt();receipt.usage={'input_tokens':2,'output_tokens':3,'total_tokens':None}
        with self.lease.bind(),model_budget(self.lease,self.run,20,receipt):pass
        row=self.store.query('SELECT state,used_tokens FROM workflow_model_reservations')[0]
        self.assertEqual(row['state'],'settled_derived');self.assertEqual(row['used_tokens'],5)
        self.assertIsNone(receipt.usage['total_tokens'],'Do not invent an upstream total receipt')
        with self.lease.bind(),model_budget(self.lease,self.run,30,Receipt()):pass
        unknown=self.store.query("SELECT used_tokens,reserved_tokens FROM workflow_model_reservations WHERE state='reserved'")[0]
        self.assertIsNone(unknown['used_tokens']);self.assertEqual(unknown['reserved_tokens'],30)
        with self.lease.bind(),self.assertRaises(TaskQuotaReached),model_budget(self.lease,self.run,1000,Receipt()):pass
        self.assertEqual(self.store.query('SELECT COUNT(*) AS n FROM workflow_model_reservations')[0]['n'],2)

    def test_zero_output_receipt_settles_input_and_retains_only_output_envelope(self):
        from webapp.workflow_agents import model_budget
        from webapp.model_adapters import get
        from webapp.task_usage import model_call
        from tests.test_receipt_output import payload
        self.run['authorization']['model_token_budget']=100000
        self.store.execute('UPDATE runs SET authorization_json=? WHERE id=?',(graph.encode(self.run['authorization']),self.run['id']))
        for protocol in ('anthropic','chat_completions','responses'):
            with self.subTest(protocol=protocol):
                receipt=get(protocol).receipt()
                with self.lease.bind(),model_budget(self.lease,self.run,18000,receipt,
                        input_reserved_tokens=6000,output_reserved_tokens=12000,method='test_estimate') as reservation, \
                        model_call(self.lease,protocol,observer=receipt):
                    receipt.feed(json.dumps(payload(protocol)).encode())
                row=self.store.query('SELECT * FROM workflow_model_reservations WHERE id=?',(reservation,))[0]
                self.assertEqual(row['state'],'partially_settled_output_unknown')
                self.assertEqual(row['reserved_tokens'],18000)
                self.assertEqual(row['used_tokens'],13200)
                self.assertEqual((row['input_reserved_tokens'],row['output_reserved_tokens']),(6000,12000))
                self.assertEqual(row['input_settled_tokens'],1200)
                self.assertIsNone(row['output_settled_tokens'])
        calls=self.store.query("SELECT input_tokens,output_tokens,receipt,error_code FROM usage_calls WHERE category='llm'")
        self.assertEqual(len(calls),3)
        for call in calls:
            self.assertEqual((call['input_tokens'],call['output_tokens']),(1200,0))
            self.assertEqual((call['receipt'],call['error_code']),('conflict','model_usage_zero_output'))

    def test_multimodal_generation_uses_count_then_settles_real_usage(self):
        import base64,io,threading
        from contextlib import nullcontext
        from types import SimpleNamespace
        from unittest.mock import Mock,patch
        from PIL import Image
        from tests.test_model_input_count import Response
        from webapp.workflow_agents import model
        from webapp.specialist_profiles import get
        encoded=io.BytesIO();Image.effect_noise((1024,640),80).convert('RGB').save(encoded,format='JPEG',quality=95)
        image='data:image/jpeg;base64,'+base64.b64encode(encoded.getvalue()).decode()
        self.assertGreater(len(image),100000)
        self.run['authorization']['model_token_budget']=100000
        self.store.execute('UPDATE runs SET authorization_json=? WHERE id=?',(graph.encode(self.run['authorization']),self.run['id']))
        configuration=SimpleNamespace(protocol='responses',model='frozen-model',reasoning_effort='high',response_transport='buffered',vision_tokenizer='patch32_1_2',input_token_policy='native',
            output_token_policy='phase',specialist_output_tokens={},budget={'task_models':40,'max_output_tokens':16000},credentials=lambda:('https://model.example/v1','test'))
        calls=[]
        def post(url,**kwargs):
            calls.append(url)
            if url.endswith('/input_tokens'):return nullcontext(Response({'input_tokens':2400}))
            self.assertTrue(url.endswith('/responses'))
            reservation=self.store.query('SELECT reserved_tokens FROM workflow_model_reservations')[0]
            self.assertLess(reservation['reserved_tokens'],100000)
            return nullcontext(Response({'status':'completed','usage':{'input_tokens':2400,'output_tokens':100,'total_tokens':2500},
                'output':[{'type':'message','content':[{'type':'output_text','text':'{"action":"result","files":{}}'}]}]}))
        client=Mock();client.post.side_effect=post
        host=SimpleNamespace(store=self.store,stop=threading.Event())
        with self.lease.bind(),patch('webapp.agent_profiles.for_specialist',return_value=configuration), \
                patch('webapp.egress_policy.session',side_effect=lambda:nullcontext(client)):
            reply=model(host,self.lease,self.run,get('media_planner'),'frozen context',[image])
        self.assertEqual(reply['action'],'result');self.assertEqual(len(calls),2)
        row=self.store.query('SELECT used_tokens,state FROM workflow_model_reservations')[0]
        self.assertEqual((row['used_tokens'],row['state']),(2500,'settled'))

    def estimated_generation(self,output_tokens=12,streaming='1',output_policy='phase',model_maximum=16000,draft_mode=False,role_maximum=None):
        from dataclasses import replace
        import threading
        from contextlib import nullcontext
        from types import SimpleNamespace
        from unittest.mock import Mock,patch
        from tests.test_model_input_count import Response
        from webapp.workflow_agents import model
        from webapp.specialist_profiles import get
        from webapp.workflow_model_protocol import ToolProtocol
        from webapp.strict_schema import transform
        request=self.store.query('SELECT * FROM planner_runs WHERE id=?',(self.task['spec']['request_id'],))[0]
        frozen={**json.loads(request['snapshot_json']),'snapshot_digest':request['snapshot_digest']}
        profile=replace(get('media_planner'),response_transport='stream' if streaming=='1' else 'buffered')
        if draft_mode:
            from webapp.workflow_plan_draft import get as get_draft
            protocol=ToolProtocol(profile,frozen,self.task,finalize=True,draft=get_draft(self.store,request['id']),store=self.store)
            tool_name='request_user_input';arguments={'reason':'Missing detail','question':'Which output?'}
        else:
            protocol=ToolProtocol(profile,frozen,self.task,finalize=True)
            tool_name='submit_plan';arguments={'kind':'REQUEST_USER_INPUT','reason':'Missing detail','question':'Which output?'}
        wire=transform(arguments,protocol.schemas[tool_name],encode=True)
        configuration=SimpleNamespace(protocol='anthropic',model='claude-4.6-sonnet',reasoning_effort='high',response_transport=profile.response_transport,
            vision_tokenizer='unconfigured',input_token_policy='claude_bpe_estimate_v1',
            output_token_policy=output_policy,specialist_output_tokens={profile.id:role_maximum} if role_maximum else {},
            budget={'task_models':40,'max_output_tokens':model_maximum},credentials=lambda:('https://model.example/v1','test'))
        self.actual_model_posts=[];self.actual_model_bodies=[]
        def post(url,**kwargs):
            self.actual_model_posts.append(url);self.actual_model_bodies.append(kwargs['json']);self.assertTrue(url.endswith('/messages'))
            rows=self.store.query('SELECT reserved_tokens,state FROM workflow_model_reservations')
            self.assertEqual(len(rows),1);self.assertEqual(rows[0]['state'],'reserved')
            self.assertGreater(rows[0]['reserved_tokens'],profile.max_output_tokens)
            return nullcontext(Response({'stop_reason':'tool_use','usage':{'input_tokens':100,'output_tokens':output_tokens},
                'content':[{'type':'tool_use','name':tool_name,'input':wire}]}))
        client=Mock();client.post.side_effect=post
        with self.lease.bind(),patch('webapp.agent_profiles.for_specialist',return_value=configuration), \
             patch('webapp.egress_policy.session',side_effect=lambda:nullcontext(client)):
            return model(SimpleNamespace(store=self.store,stop=threading.Event()),self.lease,self.run,
                profile,'Frozen context',protocol=protocol)

    def test_estimated_input_is_reserved_before_generation_and_settles_actual_usage(self):
        self.assertEqual(self.estimated_generation()['proposal']['kind'],'REQUEST_USER_INPUT')
        self.assertEqual(len(self.actual_model_posts),1)
        row=self.store.query('SELECT used_tokens,state FROM workflow_model_reservations')[0]
        self.assertEqual((row['used_tokens'],row['state']),(112,'settled_derived'))
        self.assertEqual(self.store.query("SELECT used FROM execution_budgets WHERE run_id=? AND category='model'",(self.run['id'],))[0]['used'],1)

    def test_frozen_profile_output_policy_is_sent_and_reserved_before_generation(self):
        self.estimated_generation(output_policy='profile',draft_mode=True)
        self.assertEqual(self.actual_model_bodies[0]['max_tokens'],12000)
        row=self.store.query('SELECT output_reserved_tokens,used_tokens FROM workflow_model_reservations')[0]
        self.assertEqual((row['output_reserved_tokens'],row['used_tokens']),(12000,112))

    def test_model_output_maximum_remains_a_hard_cap(self):
        self.estimated_generation(output_policy='profile',model_maximum=7000,draft_mode=True)
        self.assertEqual(self.actual_model_bodies[0]['max_tokens'],7000)
        self.assertEqual(self.store.query('SELECT output_reserved_tokens FROM workflow_model_reservations')[0]['output_reserved_tokens'],7000)

    def test_model_specific_role_override_is_sent_and_reserved(self):
        self.estimated_generation(output_policy='profile',role_maximum=16000,draft_mode=True)
        self.assertEqual(self.actual_model_bodies[0]['max_tokens'],16000)
        self.assertEqual(self.store.query('SELECT output_reserved_tokens FROM workflow_model_reservations')[0]['output_reserved_tokens'],16000)

    def test_default_phase_output_policy_keeps_compact_plan_budget(self):
        self.estimated_generation(draft_mode=True)
        self.assertEqual(self.actual_model_bodies[0]['max_tokens'],6144)

    def test_structured_generation_can_use_buffered_transport(self):
        self.assertEqual(self.estimated_generation(streaming='0')['proposal']['kind'],'REQUEST_USER_INPUT')
        self.assertEqual(self.actual_model_bodies[0]['stream'],False)

    def test_structured_generation_streams_when_enabled(self):
        self.assertEqual(self.estimated_generation(streaming='1')['proposal']['kind'],'REQUEST_USER_INPUT')
        self.assertEqual(self.actual_model_bodies[0]['stream'],True)

    def test_estimated_input_cannot_bypass_the_authorized_token_limit(self):
        self.run['authorization']['model_token_budget']=1
        self.store.execute('UPDATE runs SET authorization_json=? WHERE id=?',(graph.encode(self.run['authorization']),self.run['id']))
        with self.assertRaises(TaskQuotaReached):self.estimated_generation()
        self.assertEqual(self.actual_model_posts,[])
        self.assertFalse(self.store.query('SELECT id FROM workflow_model_reservations'))
        self.assertFalse(self.store.query("SELECT used FROM execution_budgets WHERE run_id=? AND category='model'",(self.run['id'],)))

    def test_suspect_zero_output_receipt_partially_settles_and_preserves_raw_conflict(self):
        self.assertEqual(self.estimated_generation(output_tokens=0)['proposal']['kind'],'REQUEST_USER_INPUT')
        row=self.store.query('SELECT used_tokens,reserved_tokens,state,output_reserved_tokens FROM workflow_model_reservations')[0]
        self.assertEqual(row['used_tokens'],100+row['output_reserved_tokens'])
        self.assertEqual(row['state'],'partially_settled_output_unknown');self.assertGreater(row['reserved_tokens'],100)
        usage=self.store.query("SELECT input_tokens,output_tokens,receipt,error_code FROM usage_calls WHERE category='llm'")[0]
        self.assertEqual((usage['input_tokens'],usage['output_tokens']),(100,0))
        self.assertEqual(usage['receipt'],'conflict');self.assertEqual(usage['error_code'],'model_usage_zero_output')

    def test_model_envelope_failure_preserves_actionable_safe_details(self):
        from unittest.mock import patch
        from webapp.model_adapters import PublicResponseStructureError
        from webapp.workflow_agents import AgentReplyError
        with patch('webapp.model_tool_protocol.response',side_effect=PublicResponseStructureError('Call exactly one available tool','tool_call_required')):
            with self.assertRaises(AgentReplyError) as caught:self.estimated_generation()
        self.assertEqual(caught.exception.code,'tool_call_required')
        self.assertEqual(caught.exception.details,[{'path':'tool_response','type':'tool_call_required','message':'Call exactly one available tool'}])

    def test_host_decode_failure_keeps_stage_without_exposing_exception_message(self):
        from unittest.mock import patch
        from webapp.workflow_agents import AgentReplyError
        with patch('webapp.workflow_model_protocol.ToolProtocol.decode',side_effect=ValueError('private response content')):
            with self.assertRaises(AgentReplyError) as caught:self.estimated_generation()
        self.assertEqual(caught.exception.details[0]['path'],'tool_decode')
        self.assertEqual(caught.exception.details[0]['type'],'ValueError')
        self.assertNotIn('private response content',str(caught.exception.details))

    def test_actual_usage_over_estimate_is_recorded_and_blocks_further_budget_spending(self):
        from webapp.workflow_agents import model_budget
        from webapp.usage_protocol import Receipt
        self.run['authorization']['model_token_budget']=100
        self.store.execute('UPDATE runs SET authorization_json=? WHERE id=?',(graph.encode(self.run['authorization']),self.run['id']))
        receipt=Receipt();receipt.usage={'input_tokens':110,'output_tokens':10,'total_tokens':120}
        with self.lease.bind(),model_budget(self.lease,self.run,50,receipt,input_reserved_tokens=40,output_reserved_tokens=10,method='test_estimate'):pass
        self.assertEqual(self.store.query('SELECT used_tokens FROM workflow_model_reservations')[0]['used_tokens'],120)
        with self.lease.bind(),self.assertRaises(TaskQuotaReached),model_budget(self.lease,self.run,1,Receipt()):pass

    def test_native_strict_rejection_falls_back_with_separate_receipts_and_same_snapshot(self):
        import threading
        from contextlib import nullcontext
        from types import SimpleNamespace
        from unittest.mock import Mock,patch
        from tests.test_model_input_count import Response
        from webapp.workflow_agents import model
        from webapp.specialist_profiles import get
        from webapp.workflow_model_protocol import ToolProtocol
        from webapp.strict_schema import transform
        request=self.store.query('SELECT * FROM planner_runs WHERE id=?',(self.task['spec']['request_id'],))[0]
        frozen={**json.loads(request['snapshot_json']),'snapshot_digest':request['snapshot_digest']}
        protocol=ToolProtocol(get('media_planner'),frozen,self.task,finalize=True)
        wire=transform({'kind':'REQUEST_USER_INPUT','reason':'Missing detail','question':'Which output is required?'},protocol.schemas['submit_plan'],encode=True)
        configuration=SimpleNamespace(protocol='responses',model='frozen-model',reasoning_effort='high',response_transport='buffered',vision_tokenizer='patch32_1_2',input_token_policy='native',
            output_token_policy='phase',specialist_output_tokens={},budget={'task_models':40,'max_output_tokens':16000},credentials=lambda:('https://model.example/v1','test'))
        sent=[]
        def post(url,**kwargs):
            if url.endswith('/input_tokens'):return nullcontext(Response({'input_tokens':100}))
            sent.append(kwargs['json'])
            if len(sent)==1:return nullcontext(Response({'error':{'code':'unsupported_parameter','param':'tools.0.strict'}},400))
            return nullcontext(Response({'status':'completed','usage':{'input_tokens':100,'output_tokens':20,'total_tokens':120},
                'output':[{'type':'message','phase':'commentary','content':[{'type':'output_text','text':'正在整理可执行计划。'}]},
                          {'type':'function_call','name':'submit_plan','arguments':json.dumps(wire)}]}))
        client=Mock();client.post.side_effect=post;transport={'strict':True}
        with self.lease.bind(),patch('webapp.agent_profiles.for_specialist',return_value=configuration), \
             patch('webapp.egress_policy.session',side_effect=lambda:nullcontext(client)):
            reply=model(SimpleNamespace(store=self.store,stop=threading.Event()),self.lease,self.run,get('media_planner'),
                'Frozen context',protocol=protocol,transport=transport)
        self.assertEqual(reply['proposal']['snapshot_digest'],request['snapshot_digest'])
        self.assertTrue(sent[0]['tools'][0]['strict']);self.assertIs(sent[1]['tools'][0]['strict'],False)
        self.assertEqual(sent[0]['input'],sent[1]['input']);self.assertFalse(transport['strict'])
        rows=self.store.query("SELECT status,receipt,total_tokens FROM usage_calls WHERE category='llm' ORDER BY seq")
        self.assertEqual([r['status'] for r in rows],['failed','succeeded'])
        self.assertIsNone(rows[0]['total_tokens']);self.assertEqual(rows[1]['total_tokens'],120)
        notes=[json.loads(row['payload']) for row in self.store.query("SELECT payload FROM agent_events WHERE run_id=? AND kind='note'",(self.run['id'],))]
        self.assertEqual([row['text'] for row in notes],['正在整理可执行计划。'])
        self.assertEqual(notes[0]['display_name'],'任务规划师')

    def test_accepted_strict_request_with_invalid_wire_downgrades_without_staging_bad_plan(self):
        import threading
        from contextlib import nullcontext
        from types import SimpleNamespace
        from unittest.mock import Mock,patch
        from tests.test_model_input_count import Response
        from webapp.workflow_agents import execute
        from webapp.specialist_profiles import get
        from webapp.workflow_model_protocol import ToolProtocol
        from webapp.strict_schema import transform
        request=self.store.query('SELECT * FROM planner_runs WHERE id=?',(self.task['spec']['request_id'],))[0]
        frozen={**json.loads(request['snapshot_json']),'snapshot_digest':request['snapshot_digest']}
        from webapp.workflow_plan_draft import get as get_draft
        protocol=ToolProtocol(get('media_planner'),frozen,self.task,draft=get_draft(self.store,self.task['spec']['request_id']),store=self.store)
        wire=transform({'reason':'Missing detail','question':'Which output?'},protocol.schemas['request_user_input'],encode=True)
        configuration=SimpleNamespace(protocol='responses',model='frozen-model',reasoning_effort='high',response_transport='buffered',vision_tokenizer='patch32_1_2',input_token_policy='native',
            output_token_policy='phase',specialist_output_tokens={},budget={'task_models':40,'max_output_tokens':16000},credentials=lambda:('https://model.example/v1','test'),digest='frozen')
        sent=[]
        def post(url,**kwargs):
            if url.endswith('/input_tokens'):return nullcontext(Response({'input_tokens':100}))
            sent.append(kwargs['json']);self.assertFalse(self.store.query("SELECT id FROM planner_runs WHERE status='staged'"))
            arguments={**wire,'undeclared':True} if len(sent)==1 else wire
            return nullcontext(Response({'status':'completed','usage':{'input_tokens':100,'output_tokens':20,'total_tokens':120},
                'output':[{'type':'function_call','name':'request_user_input','arguments':json.dumps(arguments)}]}))
        client=Mock();client.post.side_effect=post
        with self.lease.bind(),patch('webapp.agent_profiles.for_specialist',return_value=configuration), \
             patch('webapp.egress_policy.session',side_effect=lambda:nullcontext(client)):
            execute(SimpleNamespace(store=self.store,stop=threading.Event()),self.lease,self.run,self.task)
        self.assertEqual(len(sent),2);self.assertTrue(sent[0]['tools'][0]['strict']);self.assertIs(sent[1]['tools'][0]['strict'],False)
        saved=json.loads(self.store.query("SELECT proposal_json FROM planner_runs WHERE status='staged'")[0]['proposal_json'])
        self.assertEqual(saved['kind'],'REQUEST_USER_INPUT');self.assertNotIn('undeclared',saved)
        notes=[json.loads(row['payload'])['text'] for row in self.store.query("SELECT payload FROM agent_events WHERE run_id=? AND kind='note'",(self.run['id'],))]
        self.assertIn('Missing detail',notes)

    def test_protocols_expose_only_completed_text_not_hidden_reasoning(self):
        from webapp.model_adapters import get
        from webapp.workflow_agents import public_command_summary
        self.assertEqual(public_command_summary(None,'submit_typed_plan',{'action':'draft_batch_commit','command':{'reason':'安排本地实现'}}),'安排本地实现')
        examples={
            'responses':{'status':'completed','output':[{'type':'reasoning','summary':'PRIVATE reasoning'},
                {'type':'message','content':[{'type':'output_text','text':'{"action":"result"}'}]}]},
            'chat_completions':{'choices':[{'finish_reason':'stop','message':{'content':'{"action":"result"}','reasoning_content':'PRIVATE reasoning'}}]},
            'anthropic':{'stop_reason':'end_turn','content':[{'type':'thinking','thinking':'PRIVATE reasoning'},
                {'type':'text','text':'{"action":"result"}'}]}}
        for protocol,payload in examples.items():
            with self.subTest(protocol=protocol):
                adapter=get(protocol);body=adapter.conversation('fixed policy','task data',[])
                prepared=adapter.prepare(body,'fixed-model',1000)
                self.assertEqual(prepared['model'],'fixed-model')
                self.assertEqual(json.loads(adapter.response_text(payload)),{'action':'result'})
                self.assertNotIn('PRIVATE',adapter.response_text(payload))
        with self.assertRaises(ValueError):get('responses').response_text({'status':'incomplete'})
        with self.assertRaisesRegex(ValueError,'exactly one'):
            get('responses').response_text({'status':'completed','output':[
                {'type':'message','content':[{'type':'output_text','text':'{"action":"result"}'}]},
                {'type':'message','content':[{'type':'output_text','text':'{"action":"result"}'}]}]})
        with self.assertRaises(ValueError):get('chat_completions').response_text({'choices':[{'finish_reason':'length','message':{'content':'partial'}}]})
        with self.assertRaises(ValueError):get('anthropic').response_text({'stop_reason':'max_tokens','content':[]})

    def test_responses_selects_one_final_action_and_never_executes_commentary(self):
        from webapp.model_adapters import get,PublicResponseStructureError
        def message(phase,text):return {'type':'message','role':'assistant','status':'completed','phase':phase,'content':[{'type':'output_text','text':text}]}
        commentary=message('commentary','{"action":"script","code":"not an executable final action"}')
        final=message('final_answer','{"action":"inspect","ref":"approved"}')
        adapter=get('responses')
        payload={'status':'completed','output':[{'type':'reasoning','summary':'PRIVATE'},commentary,final]}
        self.assertEqual(json.loads(adapter.response_text(payload)),{'action':'inspect','ref':'approved'})
        self.assertEqual(adapter.public_commentary(payload),['{"action":"script","code":"not an executable final action"}'])
        self.assertNotIn('PRIVATE',''.join(adapter.public_commentary(payload)))
        for items in ([commentary],[final,final],[final,message(None,'{}')],[message('unknown','{}')],
                      [{**final,'content':final['content']*2}]):
            with self.subTest(items=items),self.assertRaises(PublicResponseStructureError):
                adapter.response_text({'status':'completed','output':items})
        with self.assertRaises(ValueError):adapter.response_text({'status':'completed','output':[{**final,'status':'incomplete'}]})
        with self.assertRaises(ValueError):adapter.response_text({'status':'completed','output':None})
        self.assertEqual(adapter.response_text({'status':'completed','output':[commentary,message(None,'{}')]}),'{}')

    def test_wrong_snapshot_echo_is_rejected_then_corrected_in_the_same_bound_request(self):
        from types import SimpleNamespace
        from unittest.mock import patch
        import threading
        from webapp.workflow_agents import execute
        proposal={'proposal_id':'bound','base_graph_version':0,'reason':'Produce the authorized report','add_tasks':[fixtures.script()],'sealed':True}
        good=self.bind_reply({'action':'proposal','proposal':proposal});bad=json.loads(json.dumps(good));bad['proposal']['snapshot_digest']='0'*64
        calls=0
        def respond(*args,**kwargs):
            nonlocal calls;calls+=1
            if calls==1:return bad
            self.assertFalse(self.store.query("SELECT id FROM planner_runs WHERE status='staged'"))
            self.assertIn('snapshot_digest',args[4]);return good
        with self.lease.bind(),patch('webapp.workflow_agents.model',side_effect=respond):
            execute(SimpleNamespace(store=self.store,stop=threading.Event()),self.lease,self.run,self.task)
        self.assertEqual(calls,2)
        saved=json.loads(self.store.query("SELECT proposal_json FROM planner_runs WHERE status='staged'")[0]['proposal_json'])
        self.assertEqual(saved['snapshot_digest'],good['proposal']['snapshot_digest'])

    def test_finalize_reserves_submission_and_does_not_execute_another_script(self):
        from dataclasses import replace
        from types import SimpleNamespace
        from unittest.mock import patch
        import threading
        from webapp.workflow_agents import execute
        from webapp.specialist_profiles import get
        policy=replace(get('editing_implementer'),max_turns=3,max_inspection_rounds=1,finalize_rounds=2)
        task={**self.task,'control_kind':'','profile':policy.snapshot(),'spec':{'profile':policy.id,
            'inputs':{'source':{'asset':'asset'}},'output_roles':{'report':'report.txt'},'timeout_seconds':900}}
        replies=[{'action':'script','code':'write output'},{'action':'script','code':'must not execute in finalize'},{'action':'result','files':{}}]
        with self.lease.bind(),patch('webapp.specialist_profiles.for_task',return_value=policy), \
             patch('webapp.workflow_agents.model',side_effect=replies) as model, \
             patch('webapp.workflow_agents.run_script',return_value=('edit/workflows/output',{'returncode':0,'outputs':[{'name':'report.txt','state':'present'}]})) as script, \
             patch('webapp.workflow_agents.finish_files',return_value='candidate') as finish:
            self.assertEqual(execute(SimpleNamespace(store=self.store,stop=threading.Event()),self.lease,self.run,task),'candidate')
        self.assertEqual(script.call_count,1);self.assertEqual(finish.call_count,1)
        self.assertIn('Script receipt (data)',model.call_args_list[-1].args[4])
        self.assertIn('No script, inspect or contract action was executed',model.call_args_list[-1].args[4])

    def test_script_failure_is_returned_to_model_before_agent_attempt_ends(self):
        from types import SimpleNamespace
        from unittest.mock import patch
        import threading
        from webapp.workflow_agents import execute
        from webapp.workflow_runtime import StepFailure
        from webapp.specialist_profiles import get
        policy=get('editing_implementer')
        task={**self.task,'control_kind':'','profile':policy.snapshot(),'spec':{'profile':policy.id,
            'inputs':{'source':{'asset':'asset'}},'output_roles':{'report':'report.txt'},'timeout_seconds':900}}
        replies=[{'action':'script','code':'raise RuntimeError("boom")'},{'action':'result','files':{}}]
        calls=[]
        def model(*args,**kwargs):
            calls.append(args[4]);return replies.pop(0)
        with self.lease.bind(),patch('webapp.workflow_agents.model',side_effect=model), \
             patch('webapp.workflow_agents.run_script',side_effect=[StepFailure({'exit_code':1,'runtime_error':'boom'}),
                 ('edit/workflows/output',{'returncode':0,'outputs':[{'name':'report.txt','state':'present'}]})]), \
             patch('webapp.workflow_agents.finish_files',return_value='candidate'):
            self.assertEqual(execute(SimpleNamespace(store=self.store,stop=threading.Event()),self.lease,self.run,task),'candidate')
        self.assertEqual(len(calls),2)
        self.assertIn('Script failed (data)',calls[1]);self.assertIn('boom',calls[1]);self.assertIn('exit_code',calls[1])

    def test_repeated_identical_script_failure_is_bounded(self):
        from types import SimpleNamespace
        from unittest.mock import patch
        import threading
        from webapp.workflow_agents import execute
        from webapp.workflow_runtime import StepFailure
        from webapp.specialist_profiles import get
        policy=get('editing_implementer')
        task={**self.task,'control_kind':'','profile':policy.snapshot(),'spec':{'profile':policy.id,
            'inputs':{'source':{'asset':'asset'}},'output_roles':{'report':'report.txt'},'timeout_seconds':900}}
        replies=[{'action':'script','code':'raise RuntimeError("boom")'}]*2
        with self.lease.bind(),patch('webapp.workflow_agents.model',side_effect=replies), \
             patch('webapp.workflow_agents.run_script',side_effect=StepFailure({'exit_code':1,'runtime_error':'boom'})):
            with self.assertRaises(StepFailure):execute(SimpleNamespace(store=self.store,stop=threading.Event()),self.lease,self.run,task)

    def test_early_correction_exhaustion_does_not_create_extra_finalize_calls(self):
        from dataclasses import replace
        from types import SimpleNamespace
        from unittest.mock import patch
        import threading
        from webapp.workflow_agents import execute,AgentReplyError
        from webapp.specialist_profiles import get
        from webapp.task_leases import TaskQuotaReached
        policy=replace(get('media_planner'),max_turns=20,max_correction_rounds=2,finalize_rounds=2)
        task={**self.task,'profile':policy.snapshot()}
        with self.lease.bind(),patch('webapp.workflow_agents.get_profile',return_value=policy), \
             patch('webapp.workflow_agents.model',side_effect=AgentReplyError('{invalid')) as model:
            with self.assertRaises(TaskQuotaReached):execute(SimpleNamespace(store=self.store,stop=threading.Event()),self.lease,self.run,task)
        self.assertEqual(model.call_count,2)
        self.assertFalse(self.store.query("SELECT id FROM planner_runs WHERE status='staged'"))

    def test_strict_transport_fallback_does_not_spend_contract_correction(self):
        from dataclasses import replace
        from types import SimpleNamespace
        from unittest.mock import patch
        import threading
        from webapp.workflow_agents import execute,AgentReplyError
        from webapp.specialist_profiles import get
        policy=replace(get('media_planner'),max_turns=6,max_correction_rounds=1,finalize_rounds=2)
        task={**self.task,'profile':policy.snapshot()}
        proposal={'proposal_id':'after-strict-fallback','base_graph_version':0,'reason':'Produce the authorized report',
                  'add_tasks':[fixtures.script()],'sealed':True}
        calls=0
        def respond(*args,**kwargs):
            nonlocal calls;calls+=1
            if calls==1:
                error=AgentReplyError('',code='invalid_tool_arguments');error.strict_not_enforced=True
                error.details=[{'path':'primary','type':'enum','message':'Value violates its declared enum constraint'}]
                raise error
            if calls==2:
                self.assertIn('"correction_rounds_remaining":1',args[4])
                self.assertFalse(kwargs['transport']['strict'])
            return self.bind_reply({'action':'proposal','proposal':proposal})
        with self.lease.bind(),patch('webapp.workflow_agents.get_profile',return_value=policy), \
             patch('webapp.workflow_agents.model',side_effect=respond):
            execute(SimpleNamespace(store=self.store,stop=threading.Event()),self.lease,self.run,task)
        self.assertEqual(calls,2)
        self.assertEqual(len(self.store.query("SELECT id FROM planner_runs WHERE status='staged'")),1)

    def test_invalid_proposal_is_corrected_before_any_durable_submission(self):
        from types import SimpleNamespace
        import threading
        from unittest.mock import patch
        from webapp.workflow_agents import execute,proposal_feedback
        invalid={'proposal_id':'corrected','base_graph_version':0,'reason':'next',
                 'add_tasks':[fixtures.script()], 'sealed':'invalid SECRET input'}
        errors=proposal_feedback(self.bind_reply({'action':'proposal','proposal':invalid})['proposal'])
        self.assertEqual(errors[0]['path'],'proposal.sealed')
        self.assertNotIn('SECRET',json.dumps(errors))
        valid={**invalid,'sealed':True}
        calls=[]
        def model(*args,**kwargs):
            calls.append(args[4])
            self.assertFalse(self.store.query('SELECT id FROM task_results'))
            self.assertFalse(self.store.query("SELECT id FROM planner_runs WHERE status='staged'"))
            return {'action':'proposal','proposal':invalid if len(calls)==1 else valid}
        host=SimpleNamespace(store=self.store,stop=threading.Event())
        with self.lease.bind(),patch('webapp.workflow_agents.model',side_effect=lambda *a,**kw:self.bind_reply(model(*a,**kw))):
            execute(host,self.lease,self.run,self.task)
        self.assertEqual(len(calls),2)
        self.assertIn('Correct only these fields in the server-owned draft',calls[1])
        self.assertEqual(len(self.store.query('SELECT id FROM task_results')),1)
        self.assertEqual(graph.get(self.store,self.run['id'])['graph_version'],0)
        self.assertEqual([r['status'] for r in self.store.query("SELECT status FROM usage_calls WHERE category='tool' AND operation='proposal' ORDER BY seq")],['failed','succeeded'])

    def test_manager_builds_server_owned_draft_before_atomic_proposal_stage(self):
        from types import SimpleNamespace
        import threading
        from unittest.mock import patch
        from webapp.workflow_agents import execute
        from webapp.strict_schema import transform
        calls=[]
        task={'id':'draft_report','type':'agent','profile':'editing_implementer','name':'draft report','objective':'Write the approved report',
              'files':[{'path':'report.txt','roles':['report']}],
              'acceptance':[{'kind':'file','stage':'final'}],'basic_reason':'Local deterministic report'}
        def respond(*args,**kwargs):
            protocol=kwargs['protocol'];calls.append(list(protocol.schemas))
            wire=transform({'tasks':[task],'kind':'COMPLETE','reason':'Draft is complete'},
                           protocol.schemas['submit_typed_plan'],encode=True)
            return protocol.decode('submit_typed_plan',wire)
        with self.lease.bind(),patch('webapp.workflow_agents.model',side_effect=respond):
            execute(SimpleNamespace(store=self.store,stop=threading.Event()),self.lease,self.run,self.task)
        row=self.store.query('SELECT status,revision,draft_json FROM workflow_plan_drafts')[0]
        self.assertEqual((row['status'],row['revision']),('COMMITTED',1))
        draft=json.loads(row['draft_json']);self.assertEqual(draft['new_tasks'][0]['id'],'draft_report')
        staged=json.loads(self.store.query("SELECT proposal_json FROM planner_runs WHERE status='staged'")[0]['proposal_json'])
        self.assertEqual(staged['proposal']['add_tasks'][0]['id'],'draft_report')
        self.assertEqual(calls,[['inspect','contract','submit_typed_plan','request_user_input','respond','request_authorization','new_run_required']])

    def test_manager_control_result_closes_its_bound_plan_draft(self):
        from types import SimpleNamespace
        import threading
        from unittest.mock import patch
        from webapp.workflow_agents import execute
        from webapp.strict_schema import transform
        def respond(*args,**kwargs):
            protocol=kwargs['protocol']
            wire=transform({'reason':'A required choice is missing','question':'Which layout should be used?','options':['compact','spacious']},
                           protocol.schemas['request_user_input'],encode=True)
            return protocol.decode('request_user_input',wire)
        with self.lease.bind(),patch('webapp.workflow_agents.model',side_effect=respond):
            execute(SimpleNamespace(store=self.store,stop=threading.Event()),self.lease,self.run,self.task)
        self.assertEqual(self.store.query('SELECT status,revision FROM workflow_plan_drafts')[0],
                         {'status':'COMMITTED','revision':0})
        staged=json.loads(self.store.query("SELECT proposal_json FROM planner_runs WHERE status='staged'")[0]['proposal_json'])
        self.assertEqual(staged['kind'],'REQUEST_USER_INPUT')

    def test_expired_manager_lease_cannot_increment_draft_error_count(self):
        from webapp.workflow_plan_draft import record_error
        from webapp.task_leases import LeaseLost
        self.store.execute('UPDATE execution_leases SET expires=0 WHERE attempt=?',(self.lease.attempt,))
        with self.assertRaises(LeaseLost):
            record_error(self.store,self.lease,self.task['spec']['request_id'],[{'path':'plan','type':'invalid'}])
        self.assertEqual(self.store.query('SELECT repeated_error_count FROM workflow_plan_drafts')[0]['repeated_error_count'],0)

    def test_plan_draft_persists_bounded_structured_compile_errors(self):
        from webapp.workflow_plan_draft import record_error
        errors=[{'path':'plan.changes.0.set_fields','type':'frozen_field','message':'delivery_stage is host-owned'}]
        with self.lease.bind():
            signature,repeated=record_error(self.store,self.lease,self.task['spec']['request_id'],errors)
        row=self.store.query('SELECT last_error_signature,last_error_json,repeated_error_count FROM workflow_plan_drafts')[0]
        self.assertEqual((row['last_error_signature'],row['repeated_error_count']),(signature,repeated))
        self.assertEqual(json.loads(row['last_error_json']),errors)

    def test_distinct_errors_without_field_details_do_not_exhaust_identical_error_limit(self):
        from types import SimpleNamespace
        import threading
        from unittest.mock import patch
        from webapp.workflow_agents import execute,AgentReplyError
        from webapp.strict_schema import transform
        calls=[]
        def respond(*args,**kwargs):
            calls.append(args[4])
            if len(calls)<=2:raise AgentReplyError('',code=('tool_call_required','ambiguous_tool_calls')[len(calls)-1])
            protocol=kwargs['protocol']
            payload={'tasks':[{'id':'report','type':'agent','profile':'editing_implementer','name':'report','objective':'Write report',
                'files':[{'path':'report.txt','roles':['report']}],'acceptance':[{'kind':'file','stage':'final'}],
                'basic_reason':'Local report'}],'kind':'COMPLETE','reason':'Corrected tool envelope'}
            return protocol.decode('submit_typed_plan',transform(payload,protocol.schemas['submit_typed_plan'],encode=True))
        with self.lease.bind(),patch('webapp.workflow_agents.model',side_effect=respond):
            execute(SimpleNamespace(store=self.store,stop=threading.Event()),self.lease,self.run,self.task)
        self.assertEqual(len(calls),3)
        self.assertIn('"type":"tool_call_required"',calls[1])
        self.assertIn('"type":"ambiguous_tool_calls"',calls[2])
        draft=self.store.query('SELECT status,repeated_error_count FROM workflow_plan_drafts')[0]
        self.assertEqual((draft['status'],draft['repeated_error_count']),('COMMITTED',0))
        records=[json.loads(r['record']) for r in self.store.query('SELECT record FROM workflow_diagnostics')]
        self.assertTrue(all(record['validation_errors'] for record in records))

    def test_identical_errors_without_field_details_still_stop_after_two(self):
        from types import SimpleNamespace
        import threading
        from unittest.mock import patch
        from webapp.workflow_agents import execute,AgentReplyError
        with self.lease.bind(),patch('webapp.workflow_agents.model',side_effect=AgentReplyError('',code='tool_call_required')) as model:
            with self.assertRaises(TaskQuotaReached):
                execute(SimpleNamespace(store=self.store,stop=threading.Event()),self.lease,self.run,self.task)
        self.assertEqual(model.call_count,2)
        draft=self.store.query('SELECT repeated_error_count,last_error_json FROM workflow_plan_drafts')[0]
        self.assertEqual(draft['repeated_error_count'],2)
        self.assertEqual(json.loads(draft['last_error_json'])[0]['type'],'tool_call_required')

    def test_typed_batch_builds_and_stages_common_plan_in_one_model_call(self):
        from types import SimpleNamespace
        import threading
        from unittest.mock import patch
        from webapp.workflow_agents import execute
        from webapp.strict_schema import transform
        task={'id':'draft_report','type':'agent','profile':'editing_implementer','name':'draft report','objective':'Write report',
              'files':[{'path':'report.txt','roles':['report']}],'acceptance':[{'kind':'file','stage':'final'}],
              'basic_reason':'Local report'}
        def respond(*args,**kwargs):
            protocol=kwargs['protocol'];payload={'tasks':[task],'kind':'COMPLETE','reason':'Complete typed batch'}
            wire=transform(payload,protocol.schemas['submit_typed_plan'],encode=True)
            return protocol.decode('submit_typed_plan',wire)
        with self.lease.bind(),patch('webapp.workflow_agents.model',side_effect=respond) as model:
            execute(SimpleNamespace(store=self.store,stop=threading.Event()),self.lease,self.run,self.task)
        self.assertEqual(model.call_count,1)
        draft=self.store.query('SELECT status,revision FROM workflow_plan_drafts')[0]
        self.assertEqual((draft['status'],draft['revision']),('COMMITTED',1))
        staged=json.loads(self.store.query("SELECT proposal_json FROM planner_runs WHERE status='staged'")[0]['proposal_json'])
        self.assertEqual(staged['proposal']['add_tasks'][0]['id'],'draft_report')

    def test_finalize_accepts_the_typed_plan_tool_after_inspection_budget_is_used(self):
        import threading
        from types import SimpleNamespace
        from unittest.mock import patch
        from webapp.workflow_agents import execute
        from webapp.strict_schema import transform
        task={'id':'final_report','type':'agent','profile':'editing_implementer','name':'Report','objective':'Write report',
              'files':[{'path':'report.txt','roles':['report']}],'acceptance':[{'kind':'file','stage':'final'}],
              'basic_reason':'The fixture verifies submission of a deterministic report file'}
        calls=[]
        def respond(*args,**kwargs):
            protocol=kwargs['protocol'];calls.append(args[4])
            if len(calls)<=9:return {'action':'inspect','ref':'asset','offset':len(calls)-1}
            self.assertIn('Finalize now',args[4]);self.assertIn('submit_typed_plan',protocol.schemas)
            self.assertNotIn('inspect',protocol.schemas)
            payload={'tasks':[task],'kind':'COMPLETE','reason':'Use the inspected evidence'}
            return protocol.decode('submit_typed_plan',transform(payload,protocol.schemas['submit_typed_plan'],encode=True))
        with self.lease.bind(),patch('webapp.workflow_agents.model',side_effect=respond), \
             patch('webapp.workflow_agents.inspect',side_effect=lambda store,run,allowed,ref,offset=0:{'ref':ref,'text':'evidence '+str(offset)}):
            execute(SimpleNamespace(store=self.store,stop=threading.Event()),self.lease,self.run,self.task)
        self.assertEqual(len(calls),10)
        self.assertEqual(self.store.query('SELECT status FROM workflow_plan_drafts')[0]['status'],'COMMITTED')
        self.assertEqual(self.store.query('SELECT status FROM planner_runs')[0]['status'],'staged')

    def test_rejection_feedback_is_consumed_once_before_new_inspections(self):
        import threading,copy
        from types import SimpleNamespace
        from unittest.mock import patch
        from webapp.workflow_agents import execute
        proposal={'proposal_id':'correct-reference','base_graph_version':0,'reason':'Use actual source',
                  'add_tasks':[fixtures.script(inputs={'source':{'asset':'asset'}})],'sealed':True}
        bad=copy.deepcopy(proposal);bad['add_tasks'][0]['inputs']['source']['asset']='invented-asset'
        replies=[{'action':'proposal','proposal':bad},{'action':'inspect','ref':'asset','offset':0},
                 {'action':'inspect','ref':'asset','offset':7},{'action':'proposal','proposal':proposal}]
        with self.lease.bind(),patch('webapp.workflow_agents.model',side_effect=[self.bind_reply(item) for item in replies]) as model, \
             patch('webapp.workflow_agents.inspect',side_effect=lambda store,run,allowed,ref,offset=0:{'ref':ref,'text':'evidence '+str(offset)}):
            execute(SimpleNamespace(store=self.store,stop=threading.Event()),self.lease,self.run,self.task)
        prompts=[call.args[4] for call in model.call_args_list]
        self.assertIn('Latest rejected plan command',prompts[1])
        self.assertIn('then call submit_typed_plan',prompts[1])
        for prompt in prompts[2:]:
            self.assertNotIn('Latest rejected plan command',prompt)
            self.assertNotIn('invented-asset',prompt)
            self.assertIn('Inspection result (data)',prompt)
        self.assertEqual(self.store.query('SELECT status FROM planner_runs')[0]['status'],'staged')

    def test_invalid_atomic_draft_is_not_persisted_and_can_be_resubmitted(self):
        from types import SimpleNamespace
        import threading
        from unittest.mock import patch
        from webapp.workflow_agents import execute,model_output_limit
        from webapp.strict_schema import transform
        base={'id':'draft_report','type':'agent','profile':'editing_implementer','name':'draft report','objective':'Write the approved report',
              'files':[{'path':'report.txt','roles':['report']}],
              'acceptance':[{'kind':'file','stage':'final'}],'basic_reason':'Local deterministic report'}
        invalid={**base,'input_bindings':[{'role':'source','ref':{'asset':'invented'}}]}
        valid={**base,'input_bindings':[{'role':'source','ref':{'asset':'asset'}}]}
        step=0;conversations=[]
        def respond(*args,**kwargs):
            nonlocal step
            protocol=kwargs['protocol'];conversations.append(args[4]);step+=1
            self.assertIn('submit_typed_plan',protocol.schemas)
            self.assertEqual(model_output_limit(args[3],protocol),6144)
            if step==2:
                row=self.store.query('SELECT revision,draft_json FROM workflow_plan_drafts')[0]
                self.assertEqual(row['revision'],0);self.assertEqual(json.loads(row['draft_json'])['new_tasks'],[])
            name='submit_typed_plan';payload={'tasks':[invalid if step==1 else valid],
                'kind':'COMPLETE','reason':'Ready' if step==1 else 'Corrected'}
            wire=transform(payload,protocol.schemas[name],encode=True);return protocol.decode(name,wire)
        with self.lease.bind(),patch('webapp.workflow_agents.model',side_effect=respond):
            execute(SimpleNamespace(store=self.store,stop=threading.Event()),self.lease,self.run,self.task)
        self.assertEqual(step,2);self.assertIn('Correct only these fields',conversations[1])
        draft=json.loads(self.store.query('SELECT draft_json FROM workflow_plan_drafts')[0]['draft_json'])
        self.assertEqual(draft['new_tasks'][0]['input_bindings'],[{'role':'source','ref':{'asset':'asset'}}])
        staged=json.loads(self.store.query("SELECT proposal_json FROM planner_runs WHERE status='staged'")[0]['proposal_json'])
        self.assertEqual(staged['proposal']['add_tasks'][0]['inputs']['source']['asset'],'asset')

    def test_compile_invalid_batch_is_feedback_and_never_persisted(self):
        from types import SimpleNamespace
        import threading
        from unittest.mock import patch
        from webapp.workflow_agents import execute
        from webapp.strict_schema import transform
        base={'id':'draft_report','type':'agent','profile':'editing_implementer','name':'draft report','objective':'Write report',
              'files':[{'path':'report.txt','roles':['report']}],'acceptance':[{'kind':'file','stage':'final'}]}
        step=0
        def respond(*args,**kwargs):
            nonlocal step;step+=1;protocol=kwargs['protocol']
            if step==2:
                self.assertIn('Typed plan was not persisted',args[4])
                row=self.store.query('SELECT revision,draft_json FROM workflow_plan_drafts')[0]
                self.assertEqual(row['revision'],0);self.assertEqual(json.loads(row['draft_json'])['new_tasks'],[])
            task={**base,**({'basic_reason':'Local report'} if step==2 else {})}
            payload={'tasks':[task],'kind':'COMPLETE','reason':'Compile batch'}
            wire=transform(payload,protocol.schemas['submit_typed_plan'],encode=True)
            return protocol.decode('submit_typed_plan',wire)
        with self.lease.bind(),patch('webapp.workflow_agents.model',side_effect=respond):
            execute(SimpleNamespace(store=self.store,stop=threading.Event()),self.lease,self.run,self.task)
        self.assertEqual(step,2)
        self.assertEqual(self.store.query('SELECT status,revision FROM workflow_plan_drafts')[0],{'status':'COMMITTED','revision':1})
        diagnostics=[json.loads(row['record']) for row in self.store.query('SELECT record FROM workflow_diagnostics')]
        rejected=next(item for item in diagnostics if item['phase']=='plan_compile')
        self.assertEqual(rejected['origin'],'model_reply')
        self.assertTrue(rejected['validation_errors'][0]['path'])
        self.assertEqual(rejected['repeated_error_count'],1)

    def test_draft_revision_change_rejects_stale_atomic_commit(self):
        from webapp.workflow_plan_draft import mutate,get as get_draft
        from webapp.workflow_model_protocol import ToolProtocol
        from webapp.specialist_profiles import get
        from webapp.strict_schema import transform
        from webapp import workflow_planning as planning
        request=self.store.query('SELECT * FROM planner_runs')[0]
        frozen={**json.loads(request['snapshot_json']),'snapshot_digest':request['snapshot_digest']}
        task={'id':'draft_report','type':'agent','profile':'editing_implementer','name':'draft report',
              'objective':'Write report','files':[{'path':'report.txt','roles':['report']}],
              'acceptance':[{'kind':'file','stage':'final'}],'basic_reason':'Local report'}
        mutate(self.store,self.lease,request['id'],'draft_add_tasks',{'tasks':[task]})
        protocol=ToolProtocol(get('media_planner'),frozen,self.task,draft=get_draft(self.store,request['id']),store=self.store,finalize=True)
        wire=transform({'kind':'COMPLETE','reason':'Ready'},protocol.schemas['commit_plan'],encode=True)
        reply=protocol.decode('commit_plan',wire)
        mutate(self.store,self.lease,request['id'],'draft_remove_tasks',{'task_ids':['draft_report']})
        with self.assertRaises(planning.ProposalSnapshotError):
            planning.stage(self.store,self.lease,request['id'],reply['proposal'],
                draft_revision=reply['_draft_revision'],draft_digest=reply['_draft_digest'])
        self.assertEqual(self.store.query('SELECT status FROM workflow_plan_drafts')[0]['status'],'OPEN')
        self.assertFalse(self.store.query("SELECT id FROM planner_runs WHERE status='staged'"))

    def test_invented_inspection_reference_is_denied_then_corrected_without_read(self):
        from types import SimpleNamespace
        import threading
        from unittest.mock import patch
        from webapp.workflow_agents import execute
        proposal={'proposal_id':'inspected','base_graph_version':0,'reason':'next',
                  'add_tasks':[fixtures.script()],'sealed':True}
        replies=[{'action':'inspect','ref':'/etc/shadow'}, {'action':'inspect','ref':'asset'},
                 {'action':'proposal','proposal':proposal}]
        host=SimpleNamespace(store=self.store,stop=threading.Event())
        with self.lease.bind(),patch('webapp.workflow_agents.model',side_effect=[self.bind_reply(reply) for reply in replies]), \
             patch('webapp.workflow_agents.inspect',return_value={'ref':'asset','text':'authorized'}) as reader:
            execute(host,self.lease,self.run,self.task)
        self.assertEqual(reader.call_count,1)
        self.assertEqual(reader.call_args.args[3],'asset')
        self.assertEqual(len(self.store.query('SELECT * FROM workflow_inspections')),1)

    def test_review_result_can_correct_reference_without_publishing_invalid_evidence(self):
        from types import SimpleNamespace
        import threading
        from unittest.mock import patch
        from webapp.workflow_agents import execute
        from webapp.specialist_profiles import get
        task={**self.task,'control_kind':'','profile':get('visual_quality_reviewer').snapshot(),
              'spec':{'profile':'visual_quality_reviewer','inputs':{'source':{'asset':'asset'}},
                      'output_roles':{'report':'review.json'},'timeout_seconds':900}}
        calls=0
        def response(*args,**kwargs):
            nonlocal calls;calls+=1
            if calls==1:
                frozen=json.loads(args[4])
                self.assertIn('submit_result',frozen['model_command_contract']['available_tools'])
                schema=kwargs['protocol'].schemas['submit_result']
                self.assertIn('verdict',schema['properties'])
                self.assertNotIn('files',schema['properties'])
                return {'action':'inspect','ref':'asset'}
            import re
            found=re.findall(r'"inspection_id":"([a-f0-9]{32})"',args[4]);actual=found[-1] if found else '0'*32
            inspection='0'*32 if calls==2 else actual
            return {'action':'result','report':{'verdict':'pass',
                'evidence':[{'criterion_index':0,'method_use':'not_applicable','observation':'Read actual input','inspection_id':inspection}]}}
        host=SimpleNamespace(store=self.store,stop=threading.Event())
        with self.lease.bind(),patch('webapp.workflow_agents.model',side_effect=response), \
             patch('webapp.workflow_agents.inspect',return_value={'ref':'asset','text':'actual input'}), \
             patch('webapp.workflow_agents.finish_files',return_value='candidate') as finish:
            self.assertEqual(execute(host,self.lease,self.run,task),'candidate')
        self.assertEqual(finish.call_count,1)
        self.assertNotEqual(json.loads(finish.call_args.args[4]['review.json'])['evidence'][0]['inspection_id'],'0'*32)
        records=self.store.query('SELECT record FROM workflow_diagnostics')
        self.assertEqual(json.loads(records[0]['record'])['phase'],'result_validation')
        self.assertFalse(self.store.query('SELECT id FROM task_results'))

    def test_public_reply_parser_accepts_only_one_json_object_and_can_correct_syntax(self):
        from webapp.workflow_agents import decode_reply,AgentReplyError,execute
        from types import SimpleNamespace
        import threading
        from unittest.mock import patch
        self.assertEqual(decode_reply('```json\n{"action":"inspect","ref":"asset"}\n```')['ref'],'asset')
        for raw in ('not JSON','{} {}','[]'):
            with self.assertRaises(AgentReplyError):decode_reply(raw)
        proposal={'proposal_id':'correct-json','base_graph_version':0,'reason':'next','add_tasks':[fixtures.script()],'sealed':True}
        host=SimpleNamespace(store=self.store,stop=threading.Event())
        with self.lease.bind(),patch('webapp.workflow_agents.model',side_effect=[AgentReplyError('{invalid'),self.bind_reply({'action':'proposal','proposal':proposal})]):
            execute(host,self.lease,self.run,self.task)
        self.assertEqual(len(self.store.query('SELECT id FROM task_results')),1)
        row=self.store.query('SELECT record FROM workflow_diagnostics')[0]
        self.assertEqual(json.loads(row['record'])['code'],'invalid_json')

    def test_video_end_timestamp_rejection_does_not_invoke_frame_decoder(self):
        from webapp.workflow_agents import inspect_media,InspectionInputError
        from types import SimpleNamespace
        from unittest.mock import patch
        import subprocess
        env={'assets':[{'id':'asset','filename':'clip.mp4'}]}
        host=SimpleNamespace(store=self.store,workflow_runner=SimpleNamespace(adapter=SimpleNamespace(environment=lambda _:env)))
        probe={'streams':[{'codec_type':'video','duration':'2.5'}],'format':{'duration':'2.5'}}
        with patch.object(self.lease,'runtime',object()),patch('webapp.agent_sandbox.command',return_value=['fixed-probe']), \
             patch('subprocess.run',return_value=subprocess.CompletedProcess([],0,json.dumps(probe).encode(),b'')) as run:
            with self.assertRaises(InspectionInputError):
                inspect_media(host,self.lease,self.run,self.task,{'ref':'asset','path':self.store.root/'unused','offset_seconds':2.5})
        self.assertEqual(run.call_count,1)
