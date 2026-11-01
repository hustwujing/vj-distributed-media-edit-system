"""Isolated indexed-log benchmark. No deployment state or external calls."""
import json
import resource
import statistics
import tempfile
import time
from pathlib import Path
from webapp.observation_collector import Archive
from webapp.telemetry import record


def main():
    with tempfile.TemporaryDirectory() as folder:
        archive=Archive(folder,max_rows=30000);count=20000;started=time.monotonic();lags=[]
        for i in range(count):
            event=record('phase.finished',task_id=f'{i%1000:032x}',phase='local',duration_seconds=.1)
            assert archive.ingest(json.dumps(event))
            lags.append(time.time()-event['time'])
        ingest=time.monotonic()-started;latencies=[]
        for i in range(100):
            start=time.monotonic();result=archive.search({'task_id':f'{i:032x}','newest':1,'limit':100});latencies.append(time.monotonic()-start)
            assert len(result['events'])==20
        start=time.monotonic()
        for _ in range(5000):record('phase.finished',task_id='a'*32,phase='local',duration_seconds=.1)
        overhead=(time.monotonic()-start)/5000
        p95=sorted(latencies)[94]
        from tests.run_workflow_suite import source_digest
        report={'source_sha256':source_digest(),'passed':p95<.25 and overhead<.001,'events':count,'tasks':1000,'query_samples':100,'query_p95_ms':round(p95*1000,3),
                'ingest_seconds':round(ingest,3),'ingest_lag_p95_ms':round(sorted(lags)[int(count*.95)]*1000,3),
                'record_construction_us':round(overhead*1e6,3),'rss_peak_mb':round(resource.getrusage(resource.RUSAGE_SELF).ru_maxrss/1024,2),
                'archive_bytes':sum(p.stat().st_size for p in Path(folder).glob('*')),
                'budget':{'query_p95_ms':250,'record_construction_us':1000},'scope':'single process archive benchmark; no claim of cross-host capacity'}
        print(json.dumps(report))
        if not report['passed']:raise SystemExit(1)


if __name__=='__main__':main()
