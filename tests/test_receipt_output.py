"""Output evidence must have the same accounting meaning on every wire protocol."""
import json
import unittest

from webapp.model_adapters import get


def payload(protocol, output=0, content=True):
    if protocol=='anthropic':
        return {'stop_reason':'tool_use','content':[{'type':'tool_use','name':'record','input':{'ok':True}}] if content else [],
                'usage':{'input_tokens':1200,'output_tokens':output}}
    if protocol=='chat_completions':
        return {'choices':[{'finish_reason':'tool_calls','message':{'tool_calls':[
                    {'type':'function','function':{'name':'record','arguments':'{}'}}]} if content else {}}],
                'usage':{'prompt_tokens':1200,'completion_tokens':output,'total_tokens':1200+output}}
    return {'status':'completed','output':[{'type':'function_call','name':'record','arguments':'{}'}] if content else [],
            'usage':{'input_tokens':1200,'output_tokens':output,'total_tokens':1200+output}}


class ReceiptOutputTests(unittest.TestCase):
    def test_json_zero_empty_and_corrected_receipts(self):
        for protocol in ('anthropic','chat_completions','responses'):
            for content in (True,False):
                with self.subTest(protocol=protocol,content=content):
                    receipt=get(protocol).receipt();raw=json.dumps(payload(protocol,content=content)).encode()
                    for byte in raw:receipt.feed(bytes([byte]))
                    receipt.finish()
                    self.assertEqual(receipt.usage['output_tokens'],0)
                    self.assertEqual(receipt.usage_anomaly,'model_usage_zero_output' if content else '')
                    self.assertEqual(receipt.buffer,b'')
                    receipt.accept(json.dumps(payload(protocol,output=20,content=False)))
                    self.assertEqual(receipt.usage_anomaly,'')
                    self.assertEqual(receipt.usage['output_tokens'],20)

    def test_sse_evidence_survives_usage_only_frames_and_any_byte_boundary(self):
        streams={
            'responses':[
                {'type':'response.output_text.delta','delta':'hello'},
                {'type':'response.function_call_arguments.delta','delta':'{}'},
                {'type':'response.custom_tool_call_input.delta','delta':'print(1)'},
                {'type':'response.reasoning_text.delta','delta':'reason'},
                {'type':'response.output_item.added','item':{'type':'function_call','name':'record'}}],
            'chat_completions':[
                {'choices':[{'delta':{key:value}}]} for key,value in (
                    ('content','hello'),('reasoning_content','reason'),('refusal','refused'),
                    ('tool_calls',[{'function':{'arguments':'{}'}}]))],
            'anthropic':[
                {'type':'content_block_delta','delta':{'type':'input_json_delta','partial_json':'{}'}}]}
        for protocol,events in streams.items():
            for event in events:
                for usage_first in (False,True):
                    with self.subTest(protocol=protocol,event=event,usage_first=usage_first):
                        receipt=get(protocol).receipt();final=payload(protocol,content=False)
                        frames=[final,event] if usage_first else [event,final]
                        raw=b''.join(b'data: '+json.dumps(frame).encode()+b'\r\n\r\n' for frame in frames)
                        for byte in raw:receipt.feed(bytes([byte]))
                        receipt.finish()
                        self.assertEqual(receipt.usage_anomaly,'model_usage_zero_output')
                        self.assertEqual(receipt.usage['output_tokens'],0)

    def test_empty_deltas_and_missing_usage_do_not_invent_conflicts(self):
        for protocol,event in (
                ('responses',{'type':'response.output_text.delta','delta':''}),
                ('chat_completions',{'choices':[{'delta':{'role':'assistant','content':''}}]}),
                ('anthropic',{'type':'content_block_delta','delta':{'text':''}})):
            with self.subTest(protocol=protocol):
                receipt=get(protocol).receipt();receipt.accept(json.dumps(event))
                self.assertEqual(receipt.usage_anomaly,'');self.assertIsNone(receipt.usage)
                receipt.accept(json.dumps(payload(protocol,content=False)))
                self.assertFalse(receipt.observed_output);self.assertEqual(receipt.usage_anomaly,'')
