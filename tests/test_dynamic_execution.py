"""Real PostgreSQL, encrypted object service, task leases and sandbox execution."""
import base64
import json
import os
from pathlib import Path
import shutil
import tempfile
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import patch
from tests.distributed_support import PostgresServer,S3Server
from tests.test_dynamic_graph import script


class ErrorClassificationTests(unittest.TestCase):
    def test_stream_protocol_resets_are_transient_model_errors(self):
        from urllib3.exceptions import ProtocolError,ReadTimeoutError
        from webapp.workflow_executor import transient_model_error
        self.assertTrue(transient_model_error(ProtocolError('stream reset')))
        self.assertTrue(transient_model_error(ReadTimeoutError(None,'/messages','timed out')))
        self.assertFalse(transient_model_error(ValueError('invalid model payload')))


@unittest.skipUnless(os.geteuid()==0 and shutil.which('bwrap'),'Requires privileged sandbox')
class DynamicExecutionTests(unittest.TestCase):
    def test_review_contract_and_model_token_exhaustion_are_distinct(self):
        from webapp.task_leases import TaskQuotaReached
        from webapp.workflow_executor import quota_error_code
        review={'control_kind':'REVIEW'}
        planner={'control_kind':'PLAN'}
        contract=TaskQuotaReached('finalize exhausted',category='agent_finalize',scope='turn',used=2,limit=2)
        tokens=TaskQuotaReached('tokens exhausted',category='model_tokens',scope='workflow',used=100,limit=100)
        self.assertEqual(quota_error_code(review,contract),'review_contract_exhausted')
        self.assertEqual(quota_error_code(planner,contract),'planning_contract_exhausted')
        self.assertEqual(quota_error_code(review,tokens),'model_token_budget_exhausted')
        self.assertEqual(quota_error_code({},contract),'budget_exhausted')

    def test_rolling_graph_executes_on_two_nodes_and_publishes_only_after_frozen_review(self):
        from webapp import agent_store,workflow_graph as graph
        from webapp.workflow_orchestrator import Orchestrator
        from webapp.workflow_runtime import Adapter
        from webapp.workflow_executor import execute
        from webapp.task_leases import Coordinator
        from webapp.execution_resources import IdentityPool
        from webapp.execution_runtime import TaskRuntime
        from webapp.artifact_store import S3Objects
        from webapp.store import Store
        pg=PostgresServer();s3=S3Server()
        try:
            with tempfile.TemporaryDirectory(prefix='dg-') as directory,patch.dict(os.environ,{
                'VIDEO_USE_DATABASE_URL':pg.dsn,'VIDEO_USE_DISTRIBUTED':'1','VIDEO_USE_OBJECT_BUCKET':'dynamic-test',
                'VIDEO_USE_OBJECT_ENDPOINT':s3.endpoint,'AWS_ACCESS_KEY_ID':'test','AWS_SECRET_ACCESS_KEY':'test',
                'AWS_DEFAULT_REGION':'us-east-1','VIDEO_USE_OBJECT_ENCRYPTION_KEY':base64.b64encode(b'd'*32).decode(),
                'VIDEO_USE_OBJECT_REQUIRE_ENCRYPTION':'1','VIDEO_USE_SESSION_SECRET':'ad'*32,
                'VIDEO_USE_TASK_MEMORY_BYTES':str(512*1024**2),'VIDEO_USE_TASK_DISK_BYTES':str(64*1024**2)}):
                root=Path(directory);S3Objects('dynamic-test',s3.endpoint).client.create_bucket(Bucket='dynamic-test')
                stores=[Store(root/'a'),Store(root/'b')]
                agent_store.initialize(stores[0]);store=stores[0]
                project=store.add_project('dynamic-tests','report')['id']
                from PIL import Image
                for identifier in ('allowed','foreign'):
                    source=store.project_dir(project)/'sources'/(identifier+'.png')
                    Image.new('RGB',(8,8),'blue').save(source)
                    store.add_asset(project,identifier,identifier+'.png',identifier+'.png',{'kind':'image','width':8,'height':8,'size':source.stat().st_size})
                hidden=store.project_dir(project)/'agent/workspace/edit/unrelated.txt'
                hidden.parent.mkdir(parents=True);hidden.write_text('unrelated project file')
                store.artifacts.save_file(project,'agent/workspace/edit/unrelated.txt')
                run=graph.create(store,project,None,{'goal':'Write and extend report','allowed_assets':['allowed','foreign'],
                    'allowed_operations':['script'],'allowed_agent_profiles':['editing_implementer'],'delivery_targets':['report'],
                    'content_fingerprint':graph.content_fingerprint(store,project)})
                turn=run['id'];self.assertFalse(store.query('SELECT id FROM agent_turns'))
                controllers=[Orchestrator(s,'controller-'+str(i)) for i,s in enumerate(stores)]
                workers=[Coordinator(s,'dynamic-node-'+str(i),'test',ttl=120) for i,s in enumerate(stores)]
                pools=[IdentityPool(root/('p'+str(i)),start=640000+i*100,size=4) for i in range(2)]
                sockets=[root/('s'+str(i)) for i in range(2)]
                for p in sockets:p.mkdir()
                hosts=[SimpleNamespace(store=s,stop=threading.Event(),workflow_runner=SimpleNamespace(adapter=Adapter(s,'test',sockets[i]))) for i,s in enumerate(stores)]
                executed=[]
                def model(host,lease,current,profile,conversation,images=(),**kwargs):
                    frozen=json.loads(conversation.split('\n',1)[0])
                    if profile.id=='media_planner':
                        first=frozen['graph_version']==0
                        code=("import os,json;from pathlib import Path;i=json.loads(os.environ['VIDEO_USE_STEP_INPUTS']);assert Path(i['source'][0]).name=='allowed.png';assert not Path('/workspace/sources/foreign.png').exists();assert not Path('/workspace/edit/unrelated.txt').exists();assert Path('/workspace/conversation.jsonl').read_text()=='';Path(os.environ['VIDEO_USE_STEP_OUTPUT'],'report.txt').write_text('first report')" if first else
                              "import os,json;from pathlib import Path;i=json.loads(os.environ['VIDEO_USE_STEP_INPUTS']);assert not list(Path('/workspace/sources').iterdir());Path(os.environ['VIDEO_USE_STEP_OUTPUT'],'report.txt').write_text(Path(i['source'][0]).read_text()+' extended')")
                        task=script('first' if first else 'second',argv=['python','-c',code])
                        task['delivery_stage']=not first
                        task['inputs']={'source':{'asset':'allowed'}} if first else {'source':{'step':'first','role':'report'}}
                        if not first:task.update(type='agent',profile='editing_implementer',argv=[])
                        return {'action':'proposal','proposal':{'proposal_id':'round-'+str(frozen['graph_version']),
                            'base_graph_version':frozen['graph_version'],'reason':'Continue from actual results','add_tasks':[task],
                            'add_dependencies':[] if first else [{'upstream':'first','downstream':'second'}],
                            'sealed':not first,'replan_after':['first'] if first else []}}
                    if profile.id=='editing_implementer':
                        receipts=[json.loads(line.split(':',1)[1]) for line in conversation.splitlines() if line.startswith('Script receipt (data):')]
                        if not receipts:return {'action':'script','code':'import json,os;print("source-path-count="+str(len(json.loads(os.environ["VIDEO_USE_STEP_INPUTS"])["source"])))'}
                        if receipts[-1]['outputs'][0]['state']=='missing':
                            self.assertIn('source-path-count=1',receipts[-1]['stdout_tail'])
                            return {'action':'script','code':"import os,json;from pathlib import Path;i=json.loads(os.environ['VIDEO_USE_STEP_INPUTS']);assert not list(Path('/workspace/sources').iterdir());Path(os.environ['VIDEO_USE_STEP_OUTPUT'],'report.txt').write_text(Path(i['source'][0]).read_text()+' extended');print('report-written')"}
                        self.assertEqual(receipts[-1]['returncode'],0)
                        self.assertEqual(receipts[-1]['outputs'][0]['size'],len('first report extended'))
                        self.assertEqual(len(receipts[-1]['outputs'][0]['sha256']),64)
                        self.assertIn('report-written',receipts[-1]['stdout_tail'])
                        self.assertEqual(receipts[-1]['acceptance'],'pending_independent_validation')
                        return {'action':'result','files':{}}
                    self.assertEqual(profile.id,'delivery_reviewer')
                    result=next(t for t in frozen['tasks'] if t['id']=='second')['accepted_result'][0]
                    if '\nInspection result (data):' not in conversation:return {'action':'inspect','ref':result}
                    self.assertIn('first report extended',conversation)
                    import re
                    inspection=re.findall(r'"inspection_id":"([a-f0-9]{32})"',conversation)[-1]
                    candidate=re.findall(r'"candidate_key":"(delivery_[0-9]+)"',conversation)[-1]
                    return {'action':'result','decision':{'snapshot_digest':frozen['snapshot_digest'],'verdict':'pass',
                        'evidence':[{'criterion_index':0,'observation':'Read the combined report text','inspection_id':inspection}],
                        'deliverables':[candidate],'primary':candidate}}
                from tests.test_dynamic_graph import envelope
                def bound_model(host,lease,*args,**kwargs):
                    reply=model(host,lease,*args,**kwargs)
                    if reply.get('action')=='proposal':reply={**reply,'proposal':envelope(host.store,lease.step_id,reply['proposal'])}
                    return reply
                with patch('webapp.workflow_agents.model',side_effect=bound_model):
                    for _ in range(20):
                        # Recover a later stage from a distinct local root.
                        index=1 if 'first' in executed else 0
                        host=hosts[index];controller=controllers[index];store=host.store
                        store.execute('UPDATE runs SET next_reconcile_at=0 WHERE id=?',(run['id'],))
                        for claim in controller.claim_due():controller.reconcile(*claim)
                        current=graph.get(store,run['id'])
                        if current['status']=='SUCCEEDED':break
                        self.assertNotIn(current['status'],('WAITING_USER','FAILED'),current)
                        for row in workers[index].candidates():
                            if not row.get('dynamic'):continue
                            lease=workers[index].claim(turn,'workflow',step_id=row['step_id'])
                            self.assertIsNotNone(lease)
                            try:
                                with lease.bind():
                                    lease.runtime_factory=lambda:TaskRuntime(lease,pools[index])
                                    execute(host,lease)
                                    staged=graph.get(store,run['id'])
                                    task=next(t for t in graph.work_items(staged) if t['id']==lease.step_id)
                                    self.assertNotEqual(task['status'],'succeeded','Executor cannot commit business success')
                            finally:
                                if lease.runtime:lease.runtime.close()
                                lease.release()
                            executed.append(row['step_id'])
                            if row['step_id']=='first':
                                self.assertFalse(graph.get(store,run['id'])['sealed'])
                                self.assertFalse(store.query("SELECT id FROM workflow_reviews WHERE purpose='final'"))
                                shutil.rmtree(stores[0].project_dir(project))
                    else:self.fail('Workflow did not converge: '+repr([(t['id'],t['status'],t['phase'],t['error_code']) for t in graph.work_items(current)]))
                self.assertEqual(current['graph_version'],2)
                self.assertEqual(current['status'],'SUCCEEDED')
                self.assertEqual(len(current['delivery']),1)
                final=store.file(project,'edit/agent_exports/'+turn+'/'+current['delivery'][0]['file'])
                self.assertEqual(final.read_text(),'first report extended')
                self.assertFalse(store.query("SELECT attempt FROM execution_cleanup WHERE state<>'complete'"))
                self.assertFalse(store.query('SELECT request_key FROM workflow_provider_calls'))
                self.assertEqual(len(store.query("SELECT seq FROM workflow_graph_events WHERE kind='delivered'")),1)
        finally:s3.close();pg.close()
