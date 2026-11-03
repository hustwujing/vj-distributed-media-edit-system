"""Usage writes and durable events remain atomic with Redis fan-out enabled."""
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from webapp import agent_store,task_usage
from webapp.store import Store

class UsageStreamTests(unittest.TestCase):
    def test_nested_rollback_never_broadcasts_uncommitted_events(self):
        with tempfile.TemporaryDirectory() as folder:
            store=Store(Path(folder));agent_store.initialize(store);project=store.add_project('owner','events')['id']
            task=agent_store.add_turn(store,project,'plan','test')
            with patch('webapp.agent_stream.bus') as bus:
                with self.assertRaises(ValueError):
                    with store.connect() as db:
                        db.execute('BEGIN IMMEDIATE')
                        agent_store.event(store,task,'progress',{'text':'not committed'})
                        bus.return_value.publish.assert_not_called()
                        raise ValueError('rollback')
                bus.return_value.publish.assert_not_called()
                self.assertEqual(store.query('SELECT seq FROM agent_events WHERE run_id=?',(task,)),[])
                with store.connect() as db:
                    db.execute('BEGIN IMMEDIATE')
                    agent_store.event(store,task,'progress',{'text':'committed'})
                    bus.return_value.publish.assert_not_called()
                self.assertEqual(bus.return_value.publish.call_count,1)

    def test_committed_tool_events_keep_stream_publication_and_one_usage_record(self):
        with tempfile.TemporaryDirectory() as folder:
            store=Store(Path(folder));agent_store.initialize(store);p=store.add_project('owner','stream')['id'];t=agent_store.add_turn(store,p,'plan','private')
            with patch('webapp.agent_stream.bus') as bus:
                agent_store.event(store,t,'tool',{'id':'tool-one','status':'running'})
                agent_store.event(store,t,'tool',{'id':'tool-one','status':'completed'})
                self.assertEqual(bus.return_value.publish.call_count,2)
                self.assertEqual(task_usage.snapshot(store,t)['counts']['tool']['calls'],1)
                agent_store.finish(store,t,'succeeded')
                bus.return_value.publish.assert_called_with(t,'terminal',{'status':'succeeded'})
                agent_store.save_text_snapshot(store,t,'snapshot','123-0')
                row=store.query('SELECT text,stream_cursor FROM agent_turns WHERE id=?',(t,))[0]
                self.assertEqual(row,{'text':'snapshot','stream_cursor':'123-0'})
            before=len(store.query('SELECT * FROM agent_events'))
            with patch('webapp.task_usage.tool',side_effect=RuntimeError('ledger unavailable')),self.assertRaises(RuntimeError):
                agent_store.event(store,t,'tool',{'id':'other','status':'running'})
            self.assertEqual(len(store.query('SELECT * FROM agent_events')),before)

    def test_real_redis_fanout_between_nodes_with_durable_usage(self):
        import os,subprocess,time,redis
        from tests.distributed_support import free_port
        from webapp.agent_stream import StreamBus
        with tempfile.TemporaryDirectory() as folder:
            port=free_port();proc=subprocess.Popen(['redis-server','--bind','127.0.0.1','--port',str(port),'--save','','--appendonly','no','--dir',folder],stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
            try:
                client=redis.Redis(host='127.0.0.1',port=port)
                for _ in range(100):
                    try:
                        if client.ping():break
                    except redis.ConnectionError:time.sleep(.02)
                else:self.fail('Redis fixture did not start')
                with patch.dict(os.environ,{'VIDEO_USE_REDIS_URL':f'redis://127.0.0.1:{port}/0'}):
                    publisher=StreamBus();reader=StreamBus()
                    store=Store(Path(folder)/'app');agent_store.initialize(store);p=store.add_project('owner','redis')['id'];task=agent_store.add_turn(store,p,'plan','private')
                    with patch('webapp.agent_stream._bus',publisher):
                        for status in ('running','completed'):agent_store.event(store,task,'tool',{'id':'tool','status':status})
                        cursor=agent_store.publish_text_delta(task,{'delta':'hello'})
                        agent_store.save_text_snapshot(store,task,'hello',cursor)
                        agent_store.finish(store,task,'succeeded','hello')
                    events=reader.read(task,'0-0',block_ms=1)
                    self.assertEqual([e['kind'] for e in events],['tool','tool','text_delta','terminal'])
                    self.assertEqual(task_usage.snapshot(store,task)['counts']['tool']['calls'],1)
                    self.assertEqual(store.query('SELECT stream_cursor FROM agent_turns WHERE id=?',(task,))[0]['stream_cursor'],cursor)
                    proc.terminate();proc.wait(timeout=5)
                    with patch('webapp.agent_stream._bus',publisher):agent_store.event(store,task,'tool',{'id':'fallback','status':'completed'})
                    self.assertEqual(task_usage.snapshot(store,task)['counts']['tool']['calls'],2)
            finally:
                if proc.poll() is None:proc.terminate();proc.wait(timeout=5)
