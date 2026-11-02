"""Cross-plan reuse authorization, exact matching and durable recovery cases."""
import copy
import hashlib
import json
import os
from pathlib import Path
import shutil
import tempfile
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from webapp import agent_store,workflows,workflow_reuse,step_checkpoints
from webapp.store import Store
from webapp.task_leases import Coordinator,LeaseLost
from webapp.workflow_runtime import Adapter
from tests.test_workflows import local_step


def pack_step(**updates):
    return {'id':'pack','name':'Pack transcript','kind':'operation','operation':'media.pack',
            'inputs':{'transcript':{'asset':'transcript'}},'parameters':{},'goal':'Pack the supplied words',
            'on_failure':'pause','acceptance':[{'kind':'text','contains':['hello']}],**updates}


def make_task(store,project,step,parent=None):
    for row in store.query("SELECT id FROM agent_turns WHERE project=? AND status IN ('queued','running','waiting','paused','cancelling')",(project,)):
        workflows.cancel(store,row['id'])
    plan=agent_store.add_turn(store,project,'plan','new version',parent_task_id=parent)
    agent_store.finish(store,plan,'succeeded','approved')
    task=agent_store.add_turn(store,project,'execute','confirmed',plan_id=plan,parent_task_id=plan)
    store.execute("UPDATE agent_turns SET status='running' WHERE id=?",(task,))
    workflows.submit(store,task,{'key':'version','name':'Version','steps':step if isinstance(step,list) else [step]})
    workflows.state(store,task,'ready')
    return task,workflows.get(store,task)


class ReuseTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name);self.store=Store(self.root/'data');agent_store.initialize(self.store)
        self.project=self.store.add_project('owner','reuse')['id'];project=self.store.project_dir(self.project)
        self.store.add_asset(self.project,'transcript','words.json','words.json',{'kind':'json'})
        (project/'sources/words.json').write_text('{"words":[]}')
        self.environment=patch('webapp.runtime_image.fingerprint',return_value='runtime-test');self.environment.start();self.addCleanup(self.environment.stop)
        self.old_task,self.old=make_task(self.store,self.project,pack_step())
        self.ref={'workflow':self.old['id'],'step':'pack','revision':0}
        self.file=f'edit/workflows/{self.old["id"]}/pack/attempt/takes_packed.md'
        self.saved=project/'step-checkpoints/source.md';self.saved.parent.mkdir(exist_ok=True);self.saved.write_bytes(b'hello')
        record={'location':str(self.saved.relative_to(self.store.root)),'size':5,'sha256':hashlib.sha256(b'hello').hexdigest()}
        self.store.execute('INSERT INTO execution_step_checkpoints VALUES (?,?,?,?,?,?,?)',
            (self.old['id'],'pack',0,self.project,'source-attempt',json.dumps({self.file:record}),self.store.now()))
        self.bindings={'_environment':'runtime-test','_reuse_contract':workflow_reuse.server_contract(),'transcript':{'asset':'transcript','path':'sources/words.json',
            'reuse_identity':{'name':'words.json','filename':'words.json'},
            'sha256':hashlib.sha256((project/'sources/words.json').read_bytes()).hexdigest(),'metadata':{'kind':'json'}}}
        self.quality={'technical_valid':True,'passed':True,'requires_agent':False,'requires_review':False,
                      'files':[{'name':self.file,'sha256':record['sha256'],'size':5,'kind':'file'}],
                      'output_roles':{'text':self.file},'checks':[]}
        workflows.change(self.store,self.old['id'],'pack',bindings=self.bindings,quality=self.quality,result=[self.file],status='succeeded')
        self.coordinator=Coordinator(self.store,'reuse-tests','test',ttl=120)

    def task(self,**updates):
        return make_task(self.store,self.project,pack_step(reuse_from=self.ref,**updates),self.old_task)

    def run_reuse(self,task,w,*,after_checkpoint=None,cancelled=lambda:False,limit=1024*1024):
        lease=self.coordinator.claim(task,'workflow',step_id=w['steps'][0]['id']);self.assertIsNotNone(lease)
        workspace=self.root/lease.attempt;workspace.mkdir();(workspace/'sources').mkdir()
        shutil.copyfile(self.store.project_dir(self.project)/'sources/words.json',workspace/'sources/words.json')
        runtime=SimpleNamespace(store=self.store,lease=lease,project=self.store.project_dir(self.project),workspace=workspace,
            path=workspace,uid=os.getuid(),gid=os.getgid(),service_uid=os.getuid(),service_gid=os.getgid(),limit=limit,
            quiesce=lambda:None)
        def checkpoint():
            step_checkpoints.save(runtime)
            if after_checkpoint:after_checkpoint()
        runtime.checkpoint=checkpoint;lease.runtime=runtime
        env={'workspace':workspace,'sources':workspace/'sources','assets':self.store.assets(self.project),
             'uid':os.getuid(),'gid':os.getgid(),'runtime':runtime}
        class TestAdapter(Adapter):
            def environment(adapter,w):return env
            def fixed_media(adapter,*args,**kwargs):raise AssertionError('Reuse must never execute the operation')
            def publish(adapter,*args,**kwargs):pass
            def validate(adapter,w,s,files):
                checked=[]
                for name in files:
                    data=(workspace/name).read_bytes()
                    checked.append({'name':name,'sha256':hashlib.sha256(data).hexdigest(),'size':len(data),'kind':'file'})
                passed=all(all(word in (workspace/f).read_text() for word in rule.get('contains',[])) for rule in s['spec']['acceptance'] for f in files)
                return {'technical_valid':True,'passed':passed,'requires_agent':False,'requires_review':False,'files':checked,'checks':[]}
        adapter=TestAdapter(self.store,'test',self.root)
        runner=workflows.Runner(self.store,adapter,threading.Event())
        try:
            with lease.bind():
                if cancelled():
                    runner.claim(w,w['steps'][0],'running')
                    return workflow_reuse.materialize(self.store,w,w['steps'][0],env,cancelled)
                runner.tick(task)
            return workflows.get(self.store,task),workspace
        finally:lease.release()

    def test_reuse_copies_pinned_result_and_revalidates_with_new_plan(self):
        task,w=self.task(id='new_pack')
        result,workspace=self.run_reuse(task,w);step=result['steps'][0]
        self.assertEqual(step['status'],'succeeded');self.assertEqual((workspace/step['result'][0]).read_text(),'hello')
        self.assertNotEqual(step['result'],[self.file]);self.assertEqual(step['bindings']['_reuse']['attempt'],'source-attempt')
        self.assertNotEqual(result['plan_id'],self.old['plan_id'])
        self.assertEqual(len(self.store.query('SELECT * FROM workflow_reuse_receipts')),2)
        self.assertEqual(workflows.public(result)['steps'][0]['reuse_from'],self.ref)

    def test_changed_acceptance_rechecks_and_blocks_downstream(self):
        task,w=make_task(self.store,self.project,[pack_step(reuse_from=self.ref,acceptance=[{'kind':'text','contains':['goodbye']}]),
            {**local_step('export',['pack']),'inputs':{'text':{'step':'pack','role':'text'}}}],self.old_task)
        result,_=self.run_reuse(task,w)
        self.assertEqual(result['steps'][0]['status'],'awaiting_review');self.assertEqual(result['status'],'paused')
        self.assertFalse(result['steps'][0]['result']);self.assertTrue(result['steps'][0]['candidate'])
        self.assertEqual(len(self.store.query('SELECT * FROM workflow_reuse_receipts')),1)
        workflows.Runner(self.store,None,threading.Event()).tick(task)
        self.assertEqual(workflows.get(self.store,task)['steps'][1]['status'],'pending')

    def test_changed_parameters_rejected_before_workflow_is_created(self):
        with self.assertRaisesRegex(ValueError,'specification mismatch'):self.task(parameters={'silence':1.0})
        self.assertEqual(len(self.store.query('SELECT id FROM agent_workflows')),1)

    def test_defaults_normalize_but_role_order_remains_significant(self):
        receipt=workflow_reuse.source_receipt(self.store,self.project,self.ref)
        spec=workflows.Step.model_validate(pack_step(parameters={'silence':.5})).model_dump()
        workflow_reuse.compare_spec(spec,receipt)
        spec['inputs']={'different_role':{'asset':'transcript'}}
        with self.assertRaisesRegex(ValueError,'roles changed'):workflow_reuse.compare_spec(spec,receipt)

    def test_changed_content_or_metadata_pauses_without_reexecution(self):
        for changed in ('content','metadata'):
            with self.subTest(changed=changed):
                task,w=self.task()
                original=self.store.project_dir(self.project)/'sources/words.json'
                if changed=='content':original.write_text('{"words":[1]}')
                else:self.store.execute('UPDATE assets SET metadata=? WHERE id=?',(json.dumps({'kind':'json','duration':2}),'transcript'))
                result,_=self.run_reuse(task,w)
                self.assertEqual(result['steps'][0]['error_code'],'reuse');self.assertEqual(result['status'],'paused')
                self.assertFalse(result['steps'][0]['candidate'])
                original.write_text('{"words":[]}')

    def test_unrelated_asset_does_not_invalidate(self):
        self.store.add_asset(self.project,'unrelated','other.json','other.json',{'kind':'json'})
        task,w=self.task();result,_=self.run_reuse(task,w)
        self.assertEqual(result['steps'][0]['status'],'succeeded')

    def test_cross_project_missing_and_superseded_sources_rejected(self):
        other=self.store.add_project('other','private')['id']
        with self.assertRaisesRegex(ValueError,'this project'):workflow_reuse.source_receipt(self.store,other,self.ref)
        with self.assertRaisesRegex(ValueError,'this project'):workflow_reuse.source_receipt(self.store,self.project,{**self.ref,'revision':99})
        self.store.execute('UPDATE agent_steps SET revision=1 WHERE workflow=?',(self.old['id'],))
        with self.assertRaisesRegex(ValueError,'superseded'):self.task()

    def test_unapproved_source_and_receipt_tampering_rejected(self):
        self.store.execute("UPDATE agent_steps SET status='awaiting_review' WHERE workflow=?",(self.old['id'],))
        with self.assertRaisesRegex(ValueError,'approved'):self.task()
        self.store.execute("UPDATE agent_steps SET status='succeeded' WHERE workflow=?",(self.old['id'],))
        self.store.execute("UPDATE workflow_reuse_receipts SET digest='invalid'")
        with self.assertRaisesRegex(ValueError,'integrity'):self.task()

    def test_corrupt_missing_and_symlink_outputs_pause(self):
        for kind in ('corrupt','missing','symlink'):
            with self.subTest(kind=kind):
                task,w=self.task()
                self.saved.unlink(missing_ok=True)
                if kind=='corrupt':self.saved.write_text('other')
                elif kind=='symlink':self.saved.symlink_to(self.store.project_dir(self.project)/'sources/words.json')
                result,_=self.run_reuse(task,w)
                self.assertEqual(result['steps'][0]['error_code'],'reuse');self.assertFalse(result['steps'][0]['result'])
                self.saved.unlink(missing_ok=True);self.saved.write_text('hello')

    def test_runtime_and_server_contract_mismatch_pause(self):
        task,w=self.task()
        with patch('webapp.runtime_image.fingerprint',return_value='runtime-changed'):
            result,_=self.run_reuse(task,w)
        self.assertEqual(result['steps'][0]['error_code'],'reuse')
        task,w=self.task()
        with patch('webapp.workflow_reuse.server_contract',return_value='changed'):
            result,_=self.run_reuse(task,w)
        self.assertEqual(result['steps'][0]['error_code'],'reuse')

    def test_source_invalidated_during_copy_does_not_commit_candidate(self):
        task,w=self.task()
        def invalidate():self.store.execute('UPDATE agent_steps SET revision=1 WHERE workflow=?',(self.old['id'],))
        result,_=self.run_reuse(task,w,after_checkpoint=invalidate)
        self.assertEqual(result['steps'][0]['error_code'],'reuse');self.assertFalse(result['steps'][0]['candidate'])

    def test_cancel_after_checkpoint_fences_publication(self):
        task,w=self.task()
        with self.assertRaises(LeaseLost):self.run_reuse(task,w,after_checkpoint=lambda:workflows.cancel(self.store,task))
        step=workflows.get(self.store,task)['steps'][0]
        self.assertFalse(step['result']);self.assertNotEqual(step['status'],'succeeded')

    def test_cancel_before_copy(self):
        task,w=self.task()
        with self.assertRaisesRegex(ValueError,'cancelled'):self.run_reuse(task,w,cancelled=lambda:True)
        self.assertFalse(workflows.get(self.store,task)['steps'][0]['candidate'])

    def test_copy_crash_recovers_to_reuse_not_original_operation(self):
        task,w=self.task();wid=w['id']
        workflows.change(self.store,wid,'pack',status='running',attempts=1)
        workflows.Runner(self.store,None,threading.Event()).recover(task)
        self.assertEqual(workflows.get(self.store,task)['steps'][0]['status'],'pending')
        result,_=self.run_reuse(task,workflows.get(self.store,task))
        self.assertEqual(result['steps'][0]['status'],'succeeded')

    def test_receipt_immutable_and_no_legacy_backfill(self):
        before=self.store.query('SELECT receipt,digest FROM workflow_reuse_receipts')
        workflows.change(self.store,self.old['id'],'pack',status='succeeded')
        self.assertEqual(before,self.store.query('SELECT receipt,digest FROM workflow_reuse_receipts'))
        self.store.execute('DELETE FROM workflow_reuse_receipts')
        with self.assertRaisesRegex(ValueError,'historical'):self.task()

    def test_scripts_and_external_generation_cannot_request_reuse(self):
        for spec in ({**local_step(),'reuse_from':self.ref},
                     {'id':'image','name':'image','kind':'operation','operation':'image.generate_from_text','parameters':{'prompt':'hello'},'reuse_from':self.ref}):
            with self.assertRaisesRegex(ValueError,'不能复用'):workflows.Step.model_validate(spec)

    def test_continuation_exposes_exact_reference_and_spec_without_authority(self):
        from webapp.continuation_context import snapshot
        task,w=self.task()
        value=snapshot(self.store,self.project,task)
        self.assertFalse(value['execution_authorized'])
        self.assertEqual(value['workflow']['steps'][0]['reuse_from'],self.ref)
        self.assertEqual(value['workflow']['steps'][0]['spec']['operation'],'media.pack')

    def test_input_signatures_preserve_role_and_dependency_content(self):
        spec={'inputs':{'first':{},'second':{}}}
        a={'path':'edit/old/x.json','sha256':'a'*64,'role':'report','metadata':{'name':'old','kind':'json'}}
        b={**a,'sha256':'b'*64}
        before=workflow_reuse.input_signature(spec,{'first':a,'second':b})
        renamed={**a,'path':'edit/new/x.json','revision':2,'step':'new','metadata':{'name':'new','kind':'json'}}
        self.assertEqual(before,workflow_reuse.input_signature(spec,{'first':renamed,'second':b}))
        self.assertNotEqual(before,workflow_reuse.input_signature(spec,{'first':b,'second':a}))

    def test_input_display_name_is_part_of_the_execution_specification(self):
        task,w=self.task()
        self.store.execute("UPDATE assets SET name='renamed.json' WHERE id='transcript'")
        result,_=self.run_reuse(task,w)
        self.assertEqual(result['steps'][0]['error_code'],'reuse')

    def test_source_binding_mutation_is_not_hidden_by_immutable_receipt(self):
        changed=copy.deepcopy(self.bindings);changed['transcript']['sha256']='0'*64
        self.store.execute('UPDATE agent_steps SET bindings=? WHERE workflow=?',(json.dumps(changed),self.old['id']))
        with self.assertRaisesRegex(ValueError,'bindings changed'):self.task()

    def test_storage_budget_and_lost_lease_prevent_publication(self):
        task,w=self.task();result,_=self.run_reuse(task,w,limit=1)
        self.assertEqual(result['steps'][0]['error_code'],'reuse')
        task,w=self.task()
        def expire():
            self.store.execute('UPDATE execution_leases SET expires=0 WHERE turn_id=?',(task,))
        with self.assertRaises(LeaseLost):self.run_reuse(task,w,after_checkpoint=expire)
        self.assertFalse(workflows.get(self.store,task)['steps'][0]['candidate'])

    def test_repeated_crash_has_bounded_recovery(self):
        task,w=self.task();workflows.change(self.store,w['id'],'pack',status='running',attempts=3)
        workflows.Runner(self.store,None,threading.Event()).recover(task)
        result=workflows.get(self.store,task)
        self.assertEqual(result['steps'][0]['error_code'],'reuse');self.assertFalse(workflows.public(result)['resumable'])

    def test_alternate_implementation_must_explicitly_opt_in(self):
        from webapp import operations,operation_lifecycle
        with patch.dict(operation_lifecycle.IMPLEMENTATIONS),patch.dict(operations._IMPLEMENTATION_BINDINGS),patch.dict(operations._VARIANT_CONTRACTS):
            implementation=operation_lifecycle.Implementation('reuse_test@2',operation_lifecycle.Lifecycle.LOCAL,'fixed_media')
            operation_lifecycle.register(implementation)
            operations.register_implementation('media.pack',implementation.id)
            self.assertFalse(operations.get('media.pack',implementation.id).reuse_safe)

    def test_project_cleanup_removes_only_its_reuse_receipts(self):
        from fastapi.testclient import TestClient
        from webapp.app import create_app
        with TestClient(create_app(self.store.root,False)) as client:
            project=client.post('/api/projects',json={'name':'cleanup reuse'}).json()['id']
            task,w=make_task(self.store,project,local_step())
            self.store.execute('INSERT INTO workflow_reuse_receipts VALUES (?,?,?,?,?,?,?)',
                (w['id'],'package',0,project,'{}','fixture',self.store.now()))
            workflows.cancel(self.store,task)
            response=client.delete('/api/projects/'+project)
            self.assertEqual(response.status_code,200,response.text)
            self.assertFalse(self.store.query('SELECT * FROM workflow_reuse_receipts WHERE project=?',(project,)))
            self.assertEqual(len(self.store.query('SELECT * FROM workflow_reuse_receipts WHERE project=?',(self.project,))),1)

    def test_explicit_media_inputs_do_not_read_undeclared_transcript_cache(self):
        from webapp.edit_tools import bind_plan
        project=self.store.project_dir(self.project)
        cached=project/'edit/transcripts/video.json';cached.parent.mkdir(exist_ok=True);cached.write_text('{"words":[]}')
        assets={'video':{'filename':'video.mp4','metadata':{'kind':'video'}}}
        raw={'clips':[{'asset':'video','start':0,'end':1}]}
        with patch('webapp.edit_tools.validate',side_effect=lambda plan,root:plan):
            legacy,_=bind_plan(raw,assets,project)
            explicit,_=bind_plan(raw,assets,project,transcript_paths={})
            declared,_=bind_plan(raw,assets,project,transcript_paths={('video',0):cached})
        self.assertTrue(legacy['transcripts']);self.assertFalse(explicit['transcripts'])
        self.assertEqual(declared['transcripts'],legacy['transcripts'])


@unittest.skipUnless(os.geteuid()==0 and shutil.which('bwrap'),'Requires isolated Linux execution')
class DistributedReuseTests(unittest.TestCase):
    def test_real_operation_then_reuse_on_another_node_and_recover_validation(self):
        from tests.distributed_support import PostgresServer,S3Server
        from webapp.artifact_store import S3Objects
        from webapp.execution_runtime import TaskRuntime
        from webapp.execution_resources import IdentityPool
        pg=PostgresServer();s3=S3Server()
        try:
            with tempfile.TemporaryDirectory(prefix='vu-reuse-shared-') as folder,patch.dict(os.environ,{
                'VIDEO_USE_DATABASE_URL':pg.dsn,'VIDEO_USE_OBJECT_ENDPOINT':s3.endpoint,'VIDEO_USE_OBJECT_BUCKET':'reuse',
                'AWS_ACCESS_KEY_ID':'test','AWS_SECRET_ACCESS_KEY':'test','AWS_DEFAULT_REGION':'us-east-1',
                'VIDEO_USE_DISTRIBUTED':'1','VIDEO_USE_TASK_DISK_BYTES':str(64*1024**2),
                'VIDEO_USE_TASK_MEMORY_BYTES':str(512*1024**2),'VIDEO_USE_NODE_CPU_SLOTS':'8',
            }):
                root=Path(folder);backend=S3Objects('reuse',s3.endpoint);backend.client.create_bucket(Bucket='reuse')
                stores=[Store(root/name) for name in ('a','b')];agent_store.initialize(stores[0])
                project=stores[0].add_project('reuse-owner','version reuse')['id']
                source=stores[0].project_dir(project)/'sources/words.json'
                source.write_text(json.dumps({'words':[{'text':'hello','start':0,'end':1,'type':'word'}]}))
                stores[0].add_asset(project,'transcript','words.json','words.json',{'kind':'json'})
                coordinators=[Coordinator(store,'reuse-'+str(i),'test',ttl=120) for i,store in enumerate(stores)]
                pools=[IdentityPool(root/('node-'+str(i)),start=780000+i*100,size=4) for i in range(2)]
                def execute(index,task,*,reuse=False,interrupt_validation=False):
                    store=stores[index];lease=coordinators[index].claim(task,'workflow',step_id='pack')
                    self.assertIsNotNone(lease)
                    lease.runtime_factory=lambda:TaskRuntime(lease,pools[index])
                    sockets=root/('sockets-'+str(index));sockets.mkdir(exist_ok=True)
                    adapter=Adapter(store,'test',sockets)
                    try:
                        with lease.bind():
                            runner=workflows.Runner(store,adapter,threading.Event())
                            runner.recover(task,step_id='pack')
                            with patch.object(adapter,'fixed_media',side_effect=AssertionError('Original operation reran')) if reuse else patch.object(adapter,'fixed_media',wraps=adapter.fixed_media):
                                if interrupt_validation:
                                    with patch.object(adapter,'validate',side_effect=RuntimeError('Validation interrupted')):runner.tick(task,step_id='pack')
                                else:runner.tick(task,step_id='pack')
                            return workflows.get(store,task)
                    finally:
                        if lease.runtime:lease.runtime.close()
                        lease.release()
                original,old=make_task(stores[0],project,pack_step())
                old=execute(0,original)
                self.assertEqual(old['steps'][0]['status'],'succeeded',old['steps'][0]['diagnostics'])
                receipt=workflow_reuse.source_receipt(stores[0],project,{'workflow':old['id'],'step':'pack','revision':0})
                self.assertEqual(len(receipt['manifest']),1)
                old_hash=old['steps'][0]['quality']['files'][0]['sha256']
                new,_=make_task(stores[1],project,pack_step(reuse_from={'workflow':old['id'],'step':'pack','revision':0}),original)
                # Lose the first node's entire cache; only committed shared data remains.
                shutil.rmtree(stores[0].project_dir(project));shutil.rmtree(stores[0].root/'cache',ignore_errors=True)
                new_state=execute(1,new,reuse=True,interrupt_validation=True)
                self.assertEqual(new_state['steps'][0]['error_code'],'validate',new_state['steps'][0]['diagnostics'])
                self.assertTrue(new_state['steps'][0]['candidate'])
                workflows.resume(stores[1],new)
                # Resume acceptance on node A without copying/reexecuting the operation.
                new_state=execute(0,new,reuse=True)
                step=new_state['steps'][0]
                self.assertEqual(step['status'],'succeeded',step['diagnostics'])
                self.assertEqual(step['quality']['files'][0]['sha256'],old_hash)
                self.assertNotEqual(step['result'],old['steps'][0]['result'])
                self.assertEqual(len(stores[1].query('SELECT * FROM workflow_reuse_receipts')),2)
                self.assertFalse(stores[1].query('SELECT * FROM workflow_provider_calls'))
                refs=stores[1].query("SELECT path FROM artifact_files WHERE project=? AND path LIKE 'reuse-receipts/%'",(project,))
                self.assertEqual(len(refs),2)
        finally:s3.close();pg.close()
