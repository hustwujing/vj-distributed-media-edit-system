import base64
from contextlib import nullcontext
import io
import json
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import Mock,patch
from PIL import Image
from webapp.model_token_estimate import estimate,tokenizer
from webapp.model_adapters import get,AnthropicReceipt
from webapp.agent_profiles import AgentProfile
from webapp.workflow_agents import input_token_reservation,ModelServiceError


class ModelTokenEstimateTests(unittest.TestCase):
    def body(self,model='claude-4.6-sonnet'):
        return {'model':model,'system':'Frozen instructions','messages':[{'role':'user','content':[
            {'type':'text','text':'中文 / English / 🙂 <|endoftext|> / code: x = {"a": 1}'}]}]}

    def image(self,quality):
        target=io.BytesIO();Image.effect_noise((1034,650),80).convert('RGB').save(target,format='JPEG',quality=quality)
        return {'type':'image','source':{'type':'base64','media_type':'image/jpeg','data':base64.b64encode(target.getvalue()).decode()}}

    def test_explicit_local_policy_requires_no_counter_network_and_remains_versioned(self):
        client=Mock();body=self.body();policy='claude_bpe_estimate_v1'
        amount,method=input_token_reservation(get('anthropic'),body,client,'https://model.example/v1','test',Mock(),time.monotonic()+10,input_token_policy=policy)
        client.post.assert_not_called();self.assertEqual(method,policy);self.assertGreater(amount,1000)
        p=AgentProfile(harness='claude_code',protocol='anthropic',model=body['model'],input_token_policy=policy)
        self.assertEqual(AgentProfile(**p.snapshot()).digest,p.digest)
        self.assertNotEqual(p.digest,AgentProfile(harness='claude_code',protocol='anthropic',model=body['model']).digest)
        with self.assertRaises(ValueError):AgentProfile(input_token_policy=policy)
        newer=self.body('claude-sonnet-5-5')
        self.assertEqual(estimate(newer,policy)['estimate_multiplier'],2.4)
        opus55=self.body('claude-opus-5-5')
        self.assertEqual(estimate(opus55,policy)['estimate_multiplier'],2.6)

    def test_images_are_counted_by_pixels_not_compressed_bytes(self):
        values=[]
        for quality in (10,95):
            body=self.body();body['messages'][0]['content'].append(self.image(quality))
            values.append(estimate(body,'claude_bpe_estimate_v1'))
        self.assertEqual(values[0],values[1]);self.assertEqual(values[0]['image_count'],1)
        body=self.body();body['messages'][0]['content']+= [self.image(50)]*8
        self.assertLess(estimate(body,'claude_bpe_estimate_v1')['estimated_input_tokens'],20000)
        body['model']='deepseek-v4.1-flash';self.assertEqual(estimate(body,'deepseek_bpe_estimate_v1')['image_envelope_tokens'],8*(1024+256))

    def test_tool_schemas_and_arguments_are_counted_as_text(self):
        body=self.body();initial=estimate(body,'claude_bpe_estimate_v1')
        body['tools']=[{'name':'synthetic','input_schema':{'type':'object','description':'long schema detail '*2000}}]
        self.assertGreater(estimate(body,'claude_bpe_estimate_v1')['reference_input_tokens'],initial['reference_input_tokens']+1000)
        body['messages'][0]['content'].append({'type':'tool_use','input':{'type':'image','source':{'data':'literal, not an image'}}})
        self.assertEqual(estimate(body,'claude_bpe_estimate_v1')['image_count'],0)

    def test_invalid_assets_and_unknown_policies_cannot_be_estimated_as_zero(self):
        with self.assertRaises(ValueError):estimate(self.body(),'unregistered')
        body=self.body();body['messages'][0]['content'].append({'type':'image','source':{'type':'url','url':'https://external.example/a.jpg'}})
        with self.assertRaises(ValueError):estimate(body,'claude_bpe_estimate_v1')
        tokenizer.cache_clear()
        try:
            with tempfile.TemporaryDirectory() as root:
                path=Path(root)/'bad';path.write_bytes(b'corrupt')
                with patch('webapp.model_token_estimate.data_path',return_value=path):
                    with self.assertRaises(ModelServiceError):input_token_reservation(get('anthropic'),self.body(),Mock(),'https://model.example','test',Mock(),time.monotonic()+10,input_token_policy='claude_bpe_estimate_v1')
        finally:tokenizer.cache_clear()

    def test_lost_lease_prevents_even_local_estimation(self):
        lease=Mock();lease.check.side_effect=RuntimeError('lease lost')
        with patch('webapp.model_token_estimate.estimate') as run:
            with self.assertRaisesRegex(RuntimeError,'lease lost'):input_token_reservation(get('anthropic'),self.body(),Mock(),'https://model.example','test',lease,time.monotonic()+10,input_token_policy='claude_bpe_estimate_v1')
            run.assert_not_called()

    def test_zero_output_receipt_is_preserved_but_not_trusted_after_actual_output(self):
        receipt=AnthropicReceipt();receipt.feed(json.dumps({'stop_reason':'tool_use','content':[{'type':'tool_use','name':'record','input':{'ok':True}}],
            'usage':{'input_tokens':100,'output_tokens':0}}).encode());receipt.finish()
        self.assertEqual(receipt.usage['output_tokens'],0);self.assertEqual(receipt.usage_anomaly,'model_usage_zero_output')
        receipt.accept(json.dumps({'usage':{'output_tokens':20}}));self.assertEqual(receipt.usage_anomaly,'')
        empty=AnthropicReceipt();empty.accept(json.dumps({'content':[],'usage':{'input_tokens':100,'output_tokens':0}}))
        self.assertEqual(empty.usage_anomaly,'')

    def test_streamed_tool_output_and_zero_final_usage_are_detected(self):
        receipt=AnthropicReceipt()
        for data in ({'type':'message_start','message':{'usage':{'input_tokens':120,'output_tokens':0}}},
                     {'type':'content_block_delta','delta':{'type':'input_json_delta','partial_json':'{"ok":true}'}},
                     {'type':'message_delta','usage':{'output_tokens':0},'delta':{'stop_reason':'tool_use'}}):
            raw=('data: '+json.dumps(data)+'\n\n').encode()
            for i in range(0,len(raw),7):receipt.feed(raw[i:i+7])
        receipt.finish();self.assertEqual(receipt.usage_anomaly,'model_usage_zero_output')
