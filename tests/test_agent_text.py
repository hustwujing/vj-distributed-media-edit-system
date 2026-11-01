import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from webapp.store import Store
from webapp import agent_store
from webapp.agent_text import TextPublisher

class TextTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.store=Store(Path(self.tmp.name));agent_store.initialize(self.store)
        self.project=self.store.add_project('owner','stream')['id'];self.task=agent_store.add_turn(self.store,self.project,'plan','test')
        self.events=[]
    def publish(self,task,kind,payload):
        self.events.append((kind,payload));return str(len(self.events))+'-0'
    def test_completed_item_flushes_the_exact_tail_before_next_model_or_tool(self):
        with patch('webapp.agent_text.bus') as bus,patch.dict(os.environ,{'VIDEO_USE_AGENT_TEXT_SNAPSHOT_SECONDS':'5'}),patch('webapp.agent_text.time.monotonic',return_value=100):
            bus.return_value.publish.side_effect=self.publish
            publisher=TextPublisher(self.store,self.task);publisher.update('我');publisher.update('我现在检查透明边缘。')
            self.assertEqual(agent_store.turn(self.store,self.task)['text'],'我')
            publisher.update('我现在检查透明边缘。',complete=True)
            row=agent_store.turn(self.store,self.task)
            self.assertEqual(row['text'],'我现在检查透明边缘。');self.assertEqual(row['text_phase'],'idle')
            self.assertEqual(self.events[-1][0],'text_snapshot');self.assertEqual(self.events[-1][1]['text'],row['text'])
            self.assertEqual([row['text_generation'],row['text_sequence']],self.events[-1][1]['text_version'])
    def test_snapshot_works_without_redis_and_completion_without_deltas(self):
        with patch('webapp.agent_text.bus') as bus:
            bus.return_value.publish.return_value=None
            publisher=TextPublisher(self.store,self.task);publisher.update('整条公开说明',complete=True)
            row=agent_store.turn(self.store,self.task);self.assertEqual(row['text'],'整条公开说明');self.assertEqual(row['text_phase'],'idle')
    def test_replacement_is_a_snapshot_and_append_has_base_version(self):
        with patch('webapp.agent_text.bus') as bus:
            bus.return_value.publish.side_effect=self.publish;publisher=TextPublisher(self.store,self.task)
            publisher.update('第一句');publisher.update('第一句\n\n第二句');publisher.update('纠正后的内容',complete=True)
            self.assertEqual([x[0] for x in self.events],['text_snapshot','text_delta','text_snapshot'])
            self.assertEqual(self.events[1][1]['delta'],'\n\n第二句');self.assertEqual(self.events[1][1]['base_version'],self.events[0][1]['text_version'])
    def test_transport_failure_is_observable_without_leaking_endpoint(self):
        from webapp.agent_stream import StreamBus
        with patch.dict(os.environ,{'VIDEO_USE_REDIS_URL':'redis://private:secret@localhost:1'}),patch('webapp.telemetry.emit') as emit:
            bus=StreamBus();bus.failed('connect',AttributeError('sensitive details'))
            self.assertFalse(bus.enabled);emit.assert_called_once_with('stream.transport_failed','WARNING',reason='connect',exception_type='AttributeError')
