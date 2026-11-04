"""Native multimodal counting fails closed before a generation request."""
import json
import time
from contextlib import nullcontext
from types import SimpleNamespace
import unittest
from unittest.mock import Mock

from webapp.model_adapters import get
from webapp.workflow_agents import input_token_reservation,ModelServiceError


class Response:
    raw=None
    def __init__(self,payload,status=200):
        self.status_code=status
        self.data=json.dumps(payload).encode()
    def iter_content(self,chunk_size):
        yield self.data


class InputCountTests(unittest.TestCase):
    def count(self,payload,status=200):
        client=Mock();client.post.return_value=nullcontext(Response(payload,status))
        lease=Mock()
        body={'model':'frozen-model','instructions':'rules','input':[{'role':'user','content':[]}],
              'max_output_tokens':2000,'stream':False}
        result=input_token_reservation(get('responses'),body,client,
            'https://model.example/v1','test',lease,time.monotonic()+10)
        return result,client

    def test_base64_size_does_not_become_the_token_reservation(self):
        (amount,method),client=self.count({'input_tokens':2400})
        self.assertEqual((amount,method),(2656,'provider_count_plus_image_envelope'))
        call=client.post.call_args
        self.assertTrue(call.args[0].endswith('/responses/input_tokens'))
        self.assertEqual(call.kwargs['json']['model'],'frozen-model')
        self.assertNotIn('max_output_tokens',call.kwargs['json'])
        self.assertFalse(call.kwargs['allow_redirects'])

    def test_missing_invalid_or_unavailable_count_is_not_zero_or_a_quota_failure(self):
        for payload,status in [({},200),({'input_tokens':True},200),({'input_tokens':-1},200),
                               ({'input_tokens':'10'},200),({'input_tokens':10},503),([],200)]:
            with self.subTest(payload=payload,status=status),self.assertRaises(ModelServiceError):
                self.count(payload,status)

    def test_text_also_uses_native_count_instead_of_serialized_utf8_length(self):
        client=Mock();client.post.return_value=nullcontext(Response({'input_tokens':1000}))
        amount,method=input_token_reservation(get('responses'),{'model':'frozen','input':'中文'*10000},client,
            'https://model.example/v1','test',Mock(),time.monotonic()+10)
        self.assertEqual((amount,method),(1256,'provider_count_plus_image_envelope'))
        client.post.assert_called_once()

    def test_image_envelope_covers_a_counter_that_ignores_pixels(self):
        import base64,io
        from PIL import Image
        from webapp.workflow_agents import image_token_envelope
        images=[]
        for quality in (10,95):
            output=io.BytesIO();Image.effect_noise((1024,640),80).convert('RGB').save(output,format='JPEG',quality=quality)
            images.append('data:image/jpeg;base64,'+base64.b64encode(output.getvalue()).decode())
        self.assertNotEqual(len(images[0]),len(images[1]))
        self.assertEqual(image_token_envelope(images[:1],'patch32_1_2'),image_token_envelope(images[1:],'patch32_1_2'))
        client=Mock();client.post.return_value=nullcontext(Response({'input_tokens':20}))
        amount,_=input_token_reservation(get('responses'),{},client,'https://model.example/v1','test',Mock(),time.monotonic()+10,images[:1],'patch32_1_2')
        self.assertGreater(amount,32*20*1.2)
        self.assertLess(amount,3000)
        self.assertLess(amount,len(images[1]))

    def test_eight_contact_sheets_use_the_frozen_model_policy_without_false_exhaustion(self):
        import base64,io
        from PIL import Image
        from webapp.workflow_agents import image_token_envelope
        image=io.BytesIO();Image.new('RGB',(1034,650),'blue').save(image,format='JPEG')
        images=['data:image/jpeg;base64,'+base64.b64encode(image.getvalue()).decode()]*8
        actual=image_token_envelope(images,'patch32_1_2')
        self.assertLess(actual,10000)
        self.assertGreater(image_token_envelope(images,'conservative_tiles512'),400000)
        with self.assertRaises(ModelServiceError):image_token_envelope(images,'unconfigured')
        self.assertEqual(image_token_envelope(images,'native'),0)
        from webapp.agent_profiles import AgentProfile
        profile=AgentProfile(vision_tokenizer='patch32_1_2')
        self.assertEqual(AgentProfile(**profile.snapshot()).vision_tokenizer,'patch32_1_2')
        self.assertNotEqual(profile.digest,AgentProfile(vision_tokenizer='native').digest)

    def test_anthropic_counter_preserves_the_exact_images_and_system_input(self):
        adapter=get('anthropic');body=adapter.conversation('rules','question',['data:image/jpeg;base64,aGVsbG8='])
        body.update(model='frozen-model',max_tokens=500,stream=False)
        path,payload=adapter.input_token_request(body)
        self.assertEqual(path,'/messages/count_tokens')
        self.assertEqual(payload,{k:body[k] for k in ('model','system','messages')})

    def test_counting_rechecks_lease_before_consuming_the_response(self):
        lease=Mock();lease.check.side_effect=[None,RuntimeError('lease lost')]
        client=Mock();client.post.return_value=nullcontext(Response({'input_tokens':10}))
        with self.assertRaisesRegex(RuntimeError,'lease lost'):
            input_token_reservation(get('responses'),{},client,'https://model.example/v1','test',lease,time.monotonic()+10)

    def test_count_telemetry_retains_components_but_not_image_or_prompt(self):
        from webapp.telemetry import record
        result=record('workflow.model_input_count',native_input_tokens=21,image_envelope_tokens=36864,
            framing_margin_tokens=256,image_count=1,prompt='private',image='private')
        self.assertEqual(result['native_input_tokens'],21)
        self.assertEqual(result['image_envelope_tokens'],36864)
        self.assertNotIn('prompt',result);self.assertNotIn('image',result)
