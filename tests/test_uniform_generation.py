import io
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import Mock, patch

import requests
from PIL import Image
from webapp import ark_generation as ark,generation_service as service,minimax_audio_generation as minimax
from webapp.generation_errors import CancellationDeferred
from webapp.operations import get, validate_parameters


CONFIG = {'VIDEO_USE_GENERATION_BACKEND': 'ark', 'VIDEO_USE_GENERATION_URL': 'https://video.example/api/v3',
          'VIDEO_USE_GENERATION_KEY': 'test-private-key', 'VIDEO_USE_GENERATION_MODEL': 'seedance-test',
          'VIDEO_USE_ARK_ACCESS_KEY':'ark-access','VIDEO_USE_ARK_SECRET_KEY':'ark-secret',
          'VIDEO_USE_ARK_PROJECT_NAME':'ark-project','VIDEO_USE_ARK_ASSET_GROUP_ID':'group-test',
          'VIDEO_USE_OBJECT_BUCKET':'test-bucket','VIDEO_USE_OBJECT_ENDPOINT':'https://objects.example'}
PARAMS = {'prompt': 'A woman runs on a sunny beach', 'width': 1280, 'height': 720, 'duration': 15, 'seed': 1}
AUDIO_CONFIG={'VIDEO_USE_AUDIO_GENERATION_URL':'https://audio.example/v1',
              'VIDEO_USE_AUDIO_GENERATION_KEY':'audio-private-key',
              'VIDEO_USE_AUDIO_GENERATION_MODEL':'speech-2.8-turbo',
              'VIDEO_USE_AUDIO_GENERATION_DOWNLOAD_HOSTS':'media.example'}
AUDIO_PARAMS={'text':'灯塔已经点亮，旅程现在开始。','voice_id':'male-qn-qingse','speed':1,'audio_format':'mp3'}


class UniformGenerationTests(unittest.TestCase):
    def test_provider_error_classification_does_not_depend_on_message_language(self):
        from webapp.generation_errors import failure_info,task_failure
        messages=['Image dimensions are invalid.','图片尺寸不符合要求。','La imagen no cumple los límites.']
        for message in messages:
            info=failure_info({'status':'failed','error':{'code':'InvalidInput','message':message}})
            self.assertEqual(info['code'],'InvalidInput');self.assertEqual(info['code_source'],'structured')
            self.assertEqual(info['provider_message'],message)
            unstructured=failure_info({'status':'failed','error':message})
            self.assertEqual(unstructured['code'],'provider_task_failed');self.assertEqual(unstructured['code_source'],'fallback')
        arbitrary='Different failure entirely; do not infer retries from this prose.'
        self.assertEqual(failure_info({'error':arbitrary})['code'],'provider_task_failed')
        flat=failure_info({'status':'failed','error_code':'UnsupportedFormat','message':arbitrary})
        self.assertEqual(flat['code'],'UnsupportedFormat');self.assertEqual(flat['provider_message'],arbitrary)
        with patch.dict(os.environ,{'PROVIDER_TEST_KEY':'private-provider-key-123'}):
            error=task_failure({'status':'failed','error':{'code':'BadRequest','message':'failed private-provider-key-123 password="private value" https://provider.invalid/?signature=private /root/private/file'}})
        serialized=json.dumps(error.diagnostic)
        for private in ('private-provider-key-123','private value','signature=private','/root/private'):
            self.assertNotIn(private,serialized)
        self.assertEqual(error.diagnostic['code'],'BadRequest')
        self.assertNotIn('provider.invalid',str(error))

    def setUp(self):
        self.env = patch.dict(os.environ, CONFIG);self.env.start();self.addCleanup(self.env.stop)

    def image(self):
        output=io.BytesIO();Image.new('RGB',(1280,720),'blue').save(output,'PNG');return output.getvalue()

    def test_three_operations_have_separate_input_contracts(self):
        self.assertEqual(get(service.TEXT_VIDEO).input_kinds,())
        self.assertEqual(get(service.IMAGE_VIDEO).input_kinds,('image',))
        self.assertEqual(validate_parameters(service.TEXT_VIDEO,PARAMS,{}),{**PARAMS,'generate_audio':False})
        with self.assertRaises(ValueError):validate_parameters(service.TEXT_VIDEO,PARAMS,{'source':{'asset':'x'}})
        with self.assertRaises(ValueError):validate_parameters(service.IMAGE_VIDEO,PARAMS,{})
        self.assertEqual(get(service.TEXT_VIDEO).public()['constraints']['durations'],list(range(4,16)))
        self.assertEqual(get(service.TEXT_VIDEO).public()['constraints']['duration_tolerance_seconds'],.1)

    def test_audio_generation_has_a_distinct_contract_and_frozen_route(self):
        with patch.dict(os.environ,AUDIO_CONFIG):
            self.assertEqual(validate_parameters(service.TEXT_AUDIO,AUDIO_PARAMS,{}),AUDIO_PARAMS)
            with self.assertRaises(ValueError):validate_parameters(service.TEXT_AUDIO,AUDIO_PARAMS,{'source':{'asset':'x'}})
            with self.assertRaises(ValueError):validate_parameters(service.TEXT_AUDIO,{**AUDIO_PARAMS,'speed':3},{})
            prepared=service.prepare(service.TEXT_AUDIO,AUDIO_PARAMS);handle=service.implementation(prepared)
            self.assertEqual(handle['protocol'],minimax.PROTOCOL);self.assertEqual(handle['audio_format'],'mp3')
            self.assertNotIn(AUDIO_CONFIG['VIDEO_USE_AUDIO_GENERATION_KEY'],json.dumps(handle))
            public=get(service.TEXT_AUDIO).public()
            self.assertTrue(public['available']);self.assertEqual(public['output_contract']['roles'],['audio'])
            self.assertEqual(public['constraints']['cancellation'],'unsupported')

    def test_audio_submit_poll_download_and_cancellation_contract(self):
        with patch.dict(os.environ,AUDIO_CONFIG):
            prepared=service.prepare(service.TEXT_AUDIO,AUDIO_PARAMS);handle=service.implementation(prepared);client=Mock()
            client.post.return_value.status_code=200;client.post.return_value.json.return_value={'task_id':'audio-task'}
            self.assertEqual(service.submit(client,prepared,'durable-key'),{'id':'audio-task','status':'waiting'})
            client.post.return_value.json.return_value={'task_id':123456}
            self.assertEqual(service.submit(client,prepared,'numeric-key'),{'id':'123456','status':'waiting'})
            self.assertEqual(client.post.call_args.args[0],'https://audio.example/v1/t2a_async_v2')
            client.get.return_value.status_code=200;client.get.return_value.json.return_value={'status':'processing'}
            self.assertEqual(service.poll(client,handle,'audio-task')['status'],'running')
            status=Mock(status_code=200);status.json.return_value={'status':'success','file_id':'file-1'}
            file_record=Mock(status_code=200);file_record.json.return_value={'file':{'download_url':'https://cdn.media.example/result.mp3?sig=private'}}
            client.get.side_effect=[status,file_record]
            result=service.poll(client,handle,'audio-task')
            self.assertEqual(result,{'status':'succeeded','artifacts':[{'id':'audio','name':'narration.mp3','url':'https://cdn.media.example/result.mp3?sig=private'}]})
            download=Mock(status_code=200,headers={'Content-Type':'audio/mpeg'})
            client.get.side_effect=None;client.get.return_value=download
            self.assertIs(service.download(client,result['artifacts'][0],handle),download)
            self.assertNotIn('headers',client.get.call_args.kwargs)
            with self.assertRaises(NotImplementedError):service.cancel(client,handle,'audio-task')
            with self.assertRaises(ValueError):minimax.validate_download_url('https://media.example.evil.test/result.mp3')

    def test_audio_frozen_credential_rejects_rotation_before_http(self):
        with patch.dict(os.environ,AUDIO_CONFIG):handle=service.implementation(service.prepare(service.TEXT_AUDIO,AUDIO_PARAMS))
        client=Mock()
        with patch.dict(os.environ,{**AUDIO_CONFIG,'VIDEO_USE_AUDIO_GENERATION_KEY':'rotated'}),self.assertRaises(ValueError):
            service.poll(client,handle,'audio-task')
        client.get.assert_not_called()

    def test_reference_is_content_addressed_and_snapshot_contains_no_bytes_or_credentials(self):
        p=service.prepare(service.IMAGE_VIDEO,PARAMS,self.image())
        self.assertEqual(len(p['references']),1);self.assertEqual(p['references'][0]['role'],'first_frame')
        self.assertEqual(p['payload']['content'],[{'type':'text','text':PARAMS['prompt']}])
        snapshot=service.implementation(p)
        self.assertEqual(len(snapshot['reference_sha256']),64)
        self.assertNotIn('payload',snapshot)
        self.assertNotIn('references',snapshot)
        self.assertNotIn('test-private-key',json.dumps(snapshot))

    def test_new_video_reference_modes_have_distinct_contracts_and_payloads(self):
        image={'asset':'image'};video={'asset':'video'};audio={'asset':'audio'}
        frames={'first_frame':image,'last_frame':image}
        self.assertEqual(set(get('video.generate_from_frames').public()['input_contract']['required_roles']),set(frames))
        validate_parameters('video.generate_from_frames',{**PARAMS,'generate_audio':True},frames)
        refs={'reference_image_1':image,'reference_audio_1':audio}
        validate_parameters('video.generate_from_references',PARAMS,refs)
        with self.assertRaises(ValueError):validate_parameters('video.generate_from_references',PARAMS,{'reference_audio_1':audio})
        validate_parameters('video.edit',{'prompt':'replace the sky','generate_audio':False},{'source':video})
        validate_parameters('video.extend',{**PARAMS,'extend_direction':'forward'},{'source':video})
        extend=get('video.extend').public()['constraints']
        self.assertEqual(extend['output_semantics'],'generated_extension_segment_only')
        self.assertIn('source start',extend['extend_direction_semantics']['backward'])

    def test_first_last_assets_are_submitted_with_frozen_roles(self):
        references={'first_frame':{'bytes':self.image()},'last_frame':{'bytes':self.image()}}
        prepared=service.prepare('video.generate_from_frames',{**PARAMS,'duration':5,'generate_audio':True},references)
        activated=[{**item,'asset_uri':'asset://asset-'+str(index)} for index,item in enumerate(prepared['references'],1)]
        client=Mock();client.post.return_value.status_code=200;client.post.return_value.json.return_value={'id':'task'}
        with patch('webapp.ark_generation.prepare_reference_assets',return_value=activated):
            self.assertEqual(service.submit(client,prepared,'key')['id'],'task')
        payload=client.post.call_args.kwargs['json']
        self.assertEqual([item.get('role') for item in payload['content'][1:]],['first_frame','last_frame'])
        self.assertTrue(payload['generate_audio'])
        self.assertTrue(all(item['image_url']['url'].startswith('asset://') for item in payload['content'][1:]))

    def test_multimodal_edit_and_extend_references_are_validated_before_provider_calls(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);video=root/'reference.mp4';audio=root/'reference.wav'
            subprocess.run(['ffmpeg','-y','-v','error','-f','lavfi','-i','testsrc2=size=854x480:rate=24',
                '-t','2','-c:v','libx264','-pix_fmt','yuv420p',str(video)],check=True,timeout=30)
            subprocess.run(['ffmpeg','-y','-v','error','-f','lavfi','-i','sine=frequency=440:sample_rate=32000',
                '-t','2','-c:a','pcm_s16le',str(audio)],check=True,timeout=30)
            references={'reference_image_1':{'bytes':self.image()},
                'reference_video_1':{'bytes':video.read_bytes()},
                'reference_audio_1':{'bytes':audio.read_bytes()}}
            multimodal=service.prepare('video.generate_from_references',{**PARAMS,'duration':5},references)
            self.assertEqual([item['kind'] for item in multimodal['references']],['image','video','audio'])
            self.assertEqual(set(multimodal['reference_sha256s']),set(references))
            source={'source':{'bytes':video.read_bytes()}}
            edited=service.prepare('video.edit',{'prompt':'replace sky'},source)
            self.assertEqual(edited['payload']['duration'],-1);self.assertEqual(edited['payload']['ratio'],'adaptive')
            self.assertTrue(edited['payload']['content'][0]['text'].startswith('严格编辑视频1'))
            extended=service.prepare('video.extend',{**PARAMS,'duration':5,'extend_direction':'backward'},source)
            self.assertEqual(extended['payload']['duration'],5);self.assertEqual(extended['payload']['ratio'],'adaptive')
            self.assertTrue(extended['payload']['content'][0]['text'].startswith('在视频1开头之前生成前序片段（backward）'))

    def test_reference_and_capability_mismatches_are_rejected_before_submission(self):
        for operation,parameters,reference in ((service.IMAGE_VIDEO,PARAMS,None),(service.TEXT_VIDEO,PARAMS,self.image()),
            (service.TEXT_VIDEO,{**PARAMS,'duration':15.5},None),(service.TEXT_VIDEO,{**PARAMS,'width':640},None),
            (service.IMAGE_VIDEO,PARAMS,b'invalid')):
            with self.subTest(operation=operation,parameters=parameters),self.assertRaises((ValueError,OSError)):
                service.prepare(operation,parameters,reference)

    def test_submit_returns_id_without_polling(self):
        client=Mock();client.post.return_value.status_code=200;client.post.return_value.json.return_value={'id':'cgt-example'}
        result=service.submit(client,service.prepare(service.TEXT_VIDEO,PARAMS),'durable-key')
        self.assertEqual(result,{'id':'cgt-example','status':'waiting'})
        self.assertEqual(client.post.call_count,1);client.get.assert_not_called()
        self.assertFalse(client.post.call_args.kwargs['allow_redirects'])

    def test_submission_transport_error_is_never_automatically_retried(self):
        client=Mock();client.post.side_effect=requests.Timeout()
        with self.assertRaises(requests.Timeout):service.submit(client,service.prepare(service.TEXT_VIDEO,PARAMS),'key')
        self.assertEqual(client.post.call_count,1)

    def test_poll_normalizes_status_and_preserves_usage(self):
        client=Mock();client.get.return_value.status_code=200;handle=service.implementation(service.prepare(service.TEXT_VIDEO,PARAMS))
        for upstream,expected in [('queued','waiting'),('running','running'),('expired','failed'),('failed','failed')]:
            client.get.return_value.json.return_value={'status':upstream,'error':{'code':'Error.Code'}}
            self.assertEqual(service.poll(client,handle,'task')['status'],expected)
        client.get.return_value.json.return_value={'status':'succeeded','usage':{'total_tokens':12},'content':{'video_url':'https://cdn.volces.com/a.mp4?signature=private'}}
        result=service.poll(client,handle,'task')
        self.assertEqual(result['artifacts'][0]['name'],'video.mp4');self.assertEqual(result['usage']['total_tokens'],12)

    def test_download_never_carries_api_auth_and_rejects_untrusted_url(self):
        client=Mock()
        service.download(client,{'url':'https://cdn.volces.com/video.mp4?signature=abc'})
        self.assertNotIn('headers',client.get.call_args.kwargs)
        self.assertFalse(client.get.call_args.kwargs['allow_redirects'])
        for url in ('http://cdn.volces.com/a','https://volces.com.evil.test/a','https://127.0.0.1/a','https://u:p@cdn.volces.com/a'):
            with self.subTest(url=url),self.assertRaises(ValueError):ark.validate_download_url(url)

    def test_configuration_change_cannot_redirect_existing_job(self):
        handle=service.implementation(service.prepare(service.TEXT_VIDEO,PARAMS))
        client=Mock();client.get.return_value.status_code=200;client.get.return_value.json.return_value={'status':'running'}
        with patch.dict(os.environ,{'VIDEO_USE_GENERATION_URL':'https://other.example','VIDEO_USE_GENERATION_MODEL':'new-default'}):
            self.assertEqual(service.poll(client,handle,'task')['status'],'running')
        self.assertEqual(client.get.call_args.args[0],CONFIG['VIDEO_USE_GENERATION_URL']+'/contents/generations/tasks/task')
        self.assertEqual(client.get.call_args.kwargs['headers']['Authorization'],'Bearer test-private-key')

    def test_frozen_video_credentials_reject_rotation_and_route_tampering_before_http(self):
        cases=[(service.TEXT_VIDEO,CONFIG,'VIDEO_USE_GENERATION_KEY',None),
               (service.IMAGE_VIDEO,{'VIDEO_USE_IMAGE_VIDEO_BACKEND':'wan','VIDEO_USE_IMAGE_VIDEO_URL':'https://wan.example/api/v1',
                'VIDEO_USE_IMAGE_VIDEO_KEY':'wan-private-key','VIDEO_USE_IMAGE_VIDEO_MODEL':'wan2.6-i2v'},'VIDEO_USE_IMAGE_VIDEO_KEY',self.image())]
        for operation,configuration,key_name,image in cases:
            with self.subTest(protocol=operation),patch.dict(os.environ,configuration):
                handle=service.implementation(service.prepare(operation,PARAMS,image))
                self.assertRegex(handle['credential_binding'],r'^[a-f0-9]{64}$')
                self.assertNotIn(configuration[key_name],json.dumps(handle))
                for changed in ({**handle,'provider':'https://untrusted.example'},
                                {**handle,'model':'changed-model'},
                                {k:v for k,v in handle.items() if k!='credential_binding'}):
                    client=Mock()
                    with self.assertRaises(ValueError):service.poll(client,changed,'task')
                    client.get.assert_not_called()
                client=Mock()
                with patch.dict(os.environ,{key_name:'different-credential'}),self.assertRaises(ValueError):
                    service.poll(client,handle,'task')
                client.get.assert_not_called()

    def test_wan_poll_uses_frozen_route_when_default_address_changes(self):
        configuration={'VIDEO_USE_IMAGE_VIDEO_BACKEND':'wan','VIDEO_USE_IMAGE_VIDEO_URL':'https://wan.example/api/v1',
            'VIDEO_USE_IMAGE_VIDEO_KEY':'wan-private-key','VIDEO_USE_IMAGE_VIDEO_MODEL':'wan2.6-i2v'}
        with patch.dict(os.environ,configuration):
            prepared=service.prepare(service.IMAGE_VIDEO,PARAMS,self.image());handle=service.implementation(prepared)
            client=Mock();client.get.return_value.status_code=200;client.get.return_value.json.return_value={'output':{'task_status':'RUNNING'}}
            with patch.dict(os.environ,{'VIDEO_USE_IMAGE_VIDEO_URL':'http://127.0.0.1:1','VIDEO_USE_IMAGE_VIDEO_MODEL':'new-default'}):
                self.assertEqual(service.poll(client,handle,'wan-id')['status'],'running')
                with self.assertRaises(ValueError):service.submit(client,prepared,'no-new-submit')
            self.assertEqual(client.get.call_args.args[0],'https://wan.example/api/v1/tasks/wan-id')
            client.post.assert_not_called()

    def test_unknown_status_and_missing_video_are_not_success(self):
        client=Mock();client.get.return_value.status_code=200;handle=service.implementation(service.prepare(service.TEXT_VIDEO,PARAMS))
        for result in ({'status':'new_state'},{'status':'succeeded','content':{}}):
            client.get.return_value.json.return_value=result
            with self.assertRaises(ValueError):service.poll(client,handle,'task')

    def test_cancel_requires_explicit_acknowledgement(self):
        client=Mock();client.get.return_value.status_code=200;handle=service.implementation(service.prepare(service.TEXT_VIDEO,PARAMS))
        client.delete.return_value.status_code=204
        self.assertEqual(service.cancel(client,handle,'task')['status'],'cancelled')
        client.delete.return_value.status_code=200
        with self.assertRaises(ValueError):service.cancel(client,handle,'task')
        client.delete.return_value.status_code=400
        client.delete.return_value.json.return_value={'error':{'code':'InvalidAction.RunningTaskDeletion'}}
        with self.assertRaises(CancellationDeferred):service.cancel(client,handle,'task')

    def test_uniform_image_result_is_inline_and_separate_from_video(self):
        with (patch('webapp.image_generation.configuration',return_value=('https://images.example/v1','private','image-model')),
              patch('webapp.image_generation.generate',return_value=(b'png',{'usage':{'tokens':1},'cost':None}))):
            prepared=service.prepare(service.TEXT_IMAGE,{'prompt':'blue','width':1024,'height':1024})
            result=service.submit(Mock(),prepared,'key')
        self.assertEqual(result['status'],'succeeded');self.assertEqual(result['artifacts'][0]['bytes'],b'png')

    def test_wan_uses_its_own_protocol_and_credentials(self):
        config={'VIDEO_USE_IMAGE_VIDEO_BACKEND':'wan','VIDEO_USE_IMAGE_VIDEO_URL':'https://wan.example/api/v1',
                'VIDEO_USE_IMAGE_VIDEO_KEY':'wan-private-key','VIDEO_USE_IMAGE_VIDEO_MODEL':'wan2.6-i2v'}
        with patch.dict(os.environ,config):
            p=service.prepare(service.IMAGE_VIDEO,PARAMS,self.image());handle=service.implementation(p)
            client=Mock();client.post.return_value.status_code=200
            client.post.return_value.json.return_value={'output':{'task_id':'wan-id'}}
            self.assertEqual(service.submit(client,p,'key')['id'],'wan-id')
            args=client.post.call_args
            self.assertEqual(args.args[0],'https://wan.example/api/v1/services/aigc/video-generation/video-synthesis')
            self.assertEqual(args.kwargs['headers']['X-DashScope-Async'],'enable')
            self.assertEqual(args.kwargs['headers']['Authorization'],'Bearer wan-private-key')
            self.assertTrue(args.kwargs['json']['input']['img_url'].startswith('data:image/png;'))
            self.assertEqual(get(service.IMAGE_VIDEO).public()['constraints']['durations'],[5,10,15])
            constraints=get(service.IMAGE_VIDEO).public()['constraints']
            self.assertEqual(constraints['reference_size_policy'],'match_output')
            self.assertEqual(constraints['camera_motion_control'],'prompt_only')
            self.assertFalse(constraints['exact_motion_guaranteed'])
            self.assertFalse(args.kwargs['json']['parameters']['prompt_extend'])
            square=io.BytesIO();Image.new('RGB',(512,512),'blue').save(square,'PNG')
            with self.assertRaisesRegex(ValueError,'参考图尺寸'):service.prepare(service.IMAGE_VIDEO,PARAMS,square.getvalue())
            self.assertEqual(client.post.call_count,1)

            client.get.return_value.status_code=200
            client.get.return_value.json.return_value={'output':{'task_status':'SUCCEEDED','video_url':'https://video.oss-cn-beijing.aliyuncs.com/a.mp4?signature=x'},'usage':{'video_duration':15}}
            with patch.dict(os.environ,{'VIDEO_USE_IMAGE_VIDEO_BACKEND':'changed'}):
                result=service.poll(client,handle,'wan-id')
            self.assertEqual(result['status'],'succeeded')
            self.assertEqual(result['usage'],{'video_duration':15})
            service.download(client,result['artifacts'][0],handle)
            self.assertNotIn('headers',client.get.call_args.kwargs)
            with self.assertRaises(NotImplementedError):service.cancel(client,handle,'wan-id')
            with self.assertRaises(ValueError):service.prepare(service.IMAGE_VIDEO,{**PARAMS,'duration':7},self.image())

    def test_rejection_diagnostics_do_not_expose_response_secrets(self):
        client=Mock();client.post.return_value.status_code=400
        client.post.return_value.json.return_value={'error':{'code':'InputImageSensitiveContentDetected.PrivacyInformation','message':'private data https://host/?secret=123'}}
        with self.assertRaises(requests.HTTPError) as error:service.submit(client,service.prepare(service.TEXT_VIDEO,PARAMS),'key')
        self.assertEqual(error.exception.provider_error_code,'InputImageSensitiveContentDetected.PrivacyInformation')
        self.assertNotIn('secret',str(error.exception));self.assertNotIn('private data',str(error.exception))
