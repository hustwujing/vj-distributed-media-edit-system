import asyncio
import json
from pathlib import Path
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

from webapp import api_capacity,telemetry
from webapp.api_execution import APIExecutors,DEFAULTS
from webapp.api_maintenance import metrics
from webapp.observation_collector import Archive


def snapshot(event,name,instance='api-a',**values):
    prefix,fields={'api.pool.snapshot':('api_pool_',api_capacity.POOL_FIELDS),
                   'api.gate.snapshot':('api_gate_',api_capacity.GATE_FIELDS),
                   'api.loop.snapshot':('api_loop_',api_capacity.LOOP_FIELDS)}[event]
    return telemetry.record(event,service='api',instance=instance,operation=name,
                            **{prefix+f:values.get(f,0) for f in fields})


class APICapacityTests(unittest.TestCase):
    def test_whitelisted_complete_snapshots_and_finite_low_cardinality_metrics(self):
        rows=[snapshot('api.pool.snapshot',p,workers=1) for p in DEFAULTS]
        rows += [snapshot('api.gate.snapshot',g,capacity=2) for g in api_capacity.GATES]
        rows += [snapshot('api.loop.snapshot','loop',default_threads_limit=40)]
        rows[0]['task_id']='private-task';rows[0]['request_id']='private-request'
        result=api_capacity.views(rows)
        self.assertEqual(result['complete_instances'],['api-a']);self.assertEqual(result['gaps'],[])
        lines='\n'.join(api_capacity.exposition(result))
        self.assertIn('video_use_api_loop_default_threads_limit{api="api-a"} 40',lines)
        self.assertNotIn('private-',lines);self.assertNotIn('task_id',lines)
        missing=api_capacity.views(rows[:-1]);self.assertTrue(missing['gaps'][0]['missing_loop'])
        self.assertEqual(missing['complete_instances'],[])
        for value in (-1,float('inf'),float('nan'),True,'1'):
            bad=dict(rows[0],api_pool_active=value);self.assertIsNone(api_capacity.metric_key(bad))
        self.assertIsNone(api_capacity.metric_key(dict(rows[0],operation='arbitrary-user-pool')))
        self.assertIsNone(api_capacity.metric_key(dict(rows[0],service='executor')))

    def test_capacity_index_rejects_duplicate_older_future_and_invalid_snapshots(self):
        with tempfile.TemporaryDirectory() as folder:
            archive=Archive(Path(folder));current=snapshot('api.pool.snapshot','read',active=2)
            self.assertTrue(archive.ingest(json.dumps(current)))
            candidates=[dict(current,api_pool_active=90),
                        dict(snapshot('api.pool.snapshot','read',active=80),time=current['time']-10),
                        dict(snapshot('api.pool.snapshot','read',active=70),time=time.time()+3600),
                        snapshot('api.pool.snapshot','read',active=-1)]
            for row in candidates:self.assertTrue(archive.ingest(json.dumps(row)))
            # Ordinary request logs do not evict the latest capacity snapshot.
            for _ in range(30):archive.ingest(json.dumps(telemetry.record('http.finished')))
            with archive.connect() as db:
                rows=db.execute('SELECT body FROM api_capacity').fetchall()
            self.assertEqual(len(rows),1);self.assertEqual(json.loads(rows[0]['body'])['api_pool_active'],2)
            fresh=snapshot('api.pool.snapshot','read',active=1);archive.ingest(json.dumps(fresh))
            with archive.connect() as db:self.assertEqual(json.loads(db.execute('SELECT body FROM api_capacity').fetchone()[0])['api_pool_active'],1)


class APICapacityProducerTests(unittest.IsolatedAsyncioTestCase):
    async def test_sampler_reports_actual_running_work_and_all_pools_without_default_threads(self):
        executors=APIExecutors();release=threading.Event();events=[]
        running=asyncio.create_task(executors.run('media',release.wait,3))
        sampler=None
        try:
            async with asyncio.timeout(1):
                while executors.pools['media'].snapshot()['active']!=1:await asyncio.sleep(.005)
            with patch('webapp.telemetry.emit',side_effect=lambda event,**kw:events.append(telemetry.record(event,instance='api-a',**kw))):
                sampler=asyncio.create_task(metrics(executors,.01));await asyncio.sleep(.025)
                sampler.cancel()
                with self.assertRaises(asyncio.CancelledError):await sampler
            self.assertEqual({e['operation'] for e in events if e['event']=='api.pool.snapshot'},set(DEFAULTS))
            self.assertTrue(all(e['api_pool_active']==1 for e in events if e['event']=='api.pool.snapshot' and e['operation']=='media'))
            self.assertTrue(all(e['api_loop_default_threads_used']==0 for e in events if e['event']=='api.loop.snapshot'))
            self.assertTrue(all(api_capacity.metric_key(e) for e in events))
        finally:
            if sampler and not sampler.done():sampler.cancel()
            release.set();await running;executors.close()
