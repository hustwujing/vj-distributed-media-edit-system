import io
import json
import os
import unittest
from unittest.mock import Mock, patch

import requests
from PIL import Image
from webapp import ark_generation as ark, generation_service as service
from webapp.operations import get, validate_parameters


CONFIG = {'VIDEO_USE_GENERATION_BACKEND': 'ark', 'VIDEO_USE_GENERATION_URL': 'https://video.example/api/v3',
          'VIDEO_USE_GENERATION_KEY': 'test-private-key', 'VIDEO_USE_GENERATION_MODEL': 'seedance-test'}
PARAMS = {'prompt': 'A woman runs on a sunny beach', 'width': 1280, 'height': 720, 'duration': 15, 'seed': 1}


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
        self.assertEqual(validate_parameters(service.TEXT_VIDEO,PARAMS,{}),PARAMS)
        with self.assertRaises(ValueError):validate_parameters(service.TEXT_VIDEO,PARAMS,{'source':{'asset':'x'}})
        with self.assertRaises(ValueError):validate_parameters(service.IMAGE_VIDEO,PARAMS,{})
        self.assertEqual(get(service.TEXT_VIDEO).public()['constraints']['durations'],list(range(4,16)))

    def test_reference_is_inline_and_snapshot_contains_no_image_or_credentials(self):
        p=service.prepare(service.IMAGE_VIDEO,PARAMS,self.image())
        self.assertEqual(p['payload']['content'][1]['role'],'first_frame')
        self.assertTrue(p['payload']['content'][1]['image_url']['url'].startswith('data:image/png;base64,'))
        snapshot=service.implementation(p)
        self.assertEqual(len(snapshot['reference_sha256']),64)
        self.assertNotIn('payload',snapshot)
        self.assertNotIn('test-private-key',json.dumps(snapshot))

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
        with patch.dict(os.environ,{'VIDEO_USE_GENERATION_URL':'https://other.example'}),self.assertRaises(ValueError):
            service.poll(Mock(),handle,'task')

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
        with self.assertRaises(NotImplementedError):service.cancel(client,handle,'task')

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
