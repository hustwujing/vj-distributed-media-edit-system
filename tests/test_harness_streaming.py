"""Public native streaming, safe SSE boundaries, and canonical text closure."""
import json
import unittest
from webapp.safe_stream import SafeEvents
from webapp.harness_sdk_events import SDKEvents


def frame(value):return ('data: '+json.dumps(value,ensure_ascii=False)+'\n\n').encode()


class NativeStreamTests(unittest.TestCase):
    def test_complete_native_text_frames_flush_without_following_packet(self):
        examples=[{'type':'content_block_delta','index':0,'delta':{'type':'text_delta','text':'透明背景，全部文字。'}},
                  {'id':'chat-1','choices':[{'index':0,'delta':{'content':'透明背景，全部文字。'}}]}]
        for message in examples:
            wire=frame(message)
            for width in (1,7,len(wire)):
                with self.subTest(protocol=message.get('type','chat'),width=width):
                    guard=SafeEvents(['protected-test-secret']);out=[]
                    for start in range(0,len(wire),width):out.extend(guard.feed(wire[start:start+width]))
                    # Assertion occurs BEFORE finish/next event. No tail buffering.
                    self.assertEqual(b''.join(out),wire)
                    self.assertEqual(list(guard.finish()),[])

    def test_split_credentials_are_blocked_in_native_text_and_tool_arguments(self):
        secret='protected-test-secret'
        constructors=[lambda text:{'type':'content_block_delta','index':0,'delta':{'type':'text_delta','text':text}},
                      lambda text:{'type':'content_block_delta','index':1,'delta':{'type':'input_json_delta','partial_json':text}},
                      lambda text:{'choices':[{'index':0,'delta':{'content':text}}]},
                      lambda text:{'choices':[{'index':0,'delta':{'tool_calls':[{'index':0,'function':{'arguments':text}}]}}]}]
        for make in constructors:
            guard=SafeEvents([secret]);self.assertEqual(list(guard.feed(frame(make(secret[:9])))),[])
            with self.assertRaises(ValueError):list(guard.feed(frame(make(secret[9:]))))

    def test_harmless_secret_prefix_released_at_native_message_boundary(self):
        cases=[({'type':'content_block_delta','index':0,'delta':{'type':'text_delta','text':'protected-'}},{'type':'message_stop'}),
               ({'choices':[{'index':0,'delta':{'content':'protected-'}}]},{'choices':[{'index':0,'delta':{},'finish_reason':'stop'}]})]
        for delta,end in cases:
            guard=SafeEvents(['protected-test-secret']);self.assertEqual(list(guard.feed(frame(delta))),[])
            self.assertEqual(b''.join(guard.feed(frame(end))),frame(delta)+frame(end))
            self.assertEqual(list(guard.finish()),[])

    def test_claude_delta_complete_and_result_share_one_public_message(self):
        events=[];codec=SDKEvents(lambda *row:events.append(row))
        codec.claude_stream({'type':'message_start','message':{'id':'reply-1'}})
        for text in ('检查透明','背景及边缘','，完成。'):
            codec.claude_stream({'type':'content_block_delta','delta':{'type':'text_delta','text':text}})
        codec.claude_message([{'type':'text','text':'检查透明背景及边缘，完成。'}])
        codec.finish('检查透明背景及边缘，完成。','completed')
        text_events=[row for row in events if row[0].startswith('message.')]
        self.assertEqual(len({row[1] for row in text_events}),1)
        self.assertEqual(''.join(row[2]['delta'] for row in events if row[0]=='message.delta'),text_events[-1][2]['text'])
        self.assertEqual(text_events[-1][2]['phase'],'final_answer')

    def test_deepseek_public_message_does_not_expose_embedded_reasoning_stream(self):
        from types import SimpleNamespace
        events=[];codec=SDKEvents(lambda *row:events.append(row))
        codec.deepseek(SimpleNamespace(method='session.event',payload={'sessionId':'root','event':{'type':'assistant/message','seq':3,
            'data':{'message':{'id':'public','content':[{'type':'thinking','thinking':'PRIVATE'},{'type':'text','text':'公开结果'}]},
                    'stream':[{'thinking':'PRIVATE'}]}}}),'root')
        codec.finish('公开结果','completed')
        self.assertNotIn('PRIVATE',json.dumps(events));self.assertEqual(events[0][2]['text'],'公开结果')
        self.assertEqual(events[0][1],events[1][1])
