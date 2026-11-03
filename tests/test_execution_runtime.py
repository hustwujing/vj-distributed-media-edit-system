"""Real Linux sandbox + UID + cgroup + checkpoint integration, no paid calls."""
import json
import os
from pathlib import Path
import shutil
import socket
import subprocess
import tempfile
import unittest
from unittest.mock import patch
from unittest.mock import Mock

from webapp import agent_store, agent_sandbox
from webapp.execution_resources import IdentityPool
from webapp.execution_runtime import TaskRuntime, copy_tree
from webapp.store import Store
from webapp.task_leases import Coordinator


class CheckpointCopyTests(unittest.TestCase):
    def test_links_and_special_files_cannot_become_checkpoint_inputs(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root/'source';source.mkdir()
            (source/'normal').write_text('ok')
            (root/'secret').write_text('secret')
            (source/'link').symlink_to(root/'secret')
            (source/'directory').symlink_to(root, target_is_directory=True)
            os.mkfifo(source/'pipe')
            records = copy_tree(source, root/'target', os.getuid(), os.getgid(), 100)
            self.assertEqual(set(records), {'normal'})
            (source/'normal').write_text('changed')
            with self.assertRaisesRegex(RuntimeError, 'integrity'):
                copy_tree(source, root/'restore', os.getuid(), os.getgid(), 100, records)


@unittest.skipUnless(os.geteuid()==0 and shutil.which('bwrap') and shutil.which('setfacl'), 'Requires Linux privileged executor prerequisites')
class RuntimeTests(unittest.TestCase):
    def test_disk_workspace_is_size_bounded_and_unmounted_after_close(self):
        from webapp.task_lifecycle import administrative
        project=self.store.add_project('disk','disk')['id']
        turn=agent_store.add_turn(self.store,project,'plan','disk workspace')
        lease=self.coordinator.claim(turn,'agent');runtime=None
        try:
            with lease.bind(),patch.dict(os.environ,{'VIDEO_USE_WORKSPACE_STORAGE':'disk','VIDEO_USE_TASK_DISK_BYTES':str(64*1024**2)}):
                runtime=TaskRuntime(lease,self.pool)
                self.assertTrue(os.path.ismount(runtime.path/'data'))
                self.assertEqual((runtime.path/'workspace.ext4').stat().st_size,64*1024**2)
                with self.assertRaises(OSError):
                    with (runtime.path/'data/too-large.bin').open('wb') as output:os.posix_fallocate(output.fileno(),0,80*1024**2)
            path=runtime.path;runtime.close();runtime=None
            self.assertFalse(path.exists())
            with administrative():self.assertEqual(self.store.query('SELECT state FROM execution_cleanup WHERE attempt=?',(lease.attempt,))[0]['state'],'complete')
        finally:
            if runtime:runtime.close()
            lease.release()

    def test_new_identity_publishes_task_and_valid_permit_atomically(self):
        import time
        from webapp.execution_resources import Identity
        project=self.store.add_project('b','permit-check')['id']
        turn=agent_store.add_turn(self.store,project,'plan','permit check')
        lease=self.coordinator.claim(turn,'agent')
        self.assertIsNotNone(lease)
        original=Identity.save;observed=[]
        def save(identity,**values):
            if 'task_id' in values:
                self.assertGreater(values.get('permit_until',0),time.monotonic())
                observed.append(values['task_id'])
            return original(identity,**values)
        runtime=None
        try:
            with lease.bind(),patch.object(Identity,'save',save):runtime=TaskRuntime(lease,self.pool)
            self.assertEqual(observed,[turn])
        finally:
            if runtime:runtime.close()
            lease.release()

    def setUp(self):
        import fcntl
        self.runtime_test_lock=open('/tmp/video-use-runtime-tests.lock','a')
        fcntl.flock(self.runtime_test_lock,fcntl.LOCK_EX)
        self.addCleanup(self.runtime_test_lock.close)
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.settings = patch.dict(os.environ, {'VIDEO_USE_TASK_DISK_BYTES':str(64*1024**2),
            'VIDEO_USE_TASK_MEMORY_BYTES':str((8192 if self._testMethodName.startswith('test_real_') else 256)*1024**2), 'VIDEO_USE_TASK_PIDS':'128' if self._testMethodName.startswith('test_real_') else '32'})
        self.settings.start()
        self.addCleanup(self.settings.stop)
        self.store = Store(self.root/'shared')
        agent_store.initialize(self.store)
        self.project = self.store.add_project('a', 'A')['id']
        plan=agent_store.add_turn(self.store,self.project,'plan','adjust image')
        self.store.execute("UPDATE agent_turns SET status='running' WHERE id=?",(plan,))
        from webapp.execution_intent import declare
        smoke_operation={'test_real_birefnet_cutout':'image.remove_background','test_real_sam2_segment':'image.segment','test_real_ocr':'image.ocr','test_real_sam3_segment':'image.segment_sam3','test_real_sam3_empty_selection':'image.segment_sam3'}.get(self._testMethodName,'image.local_adjust')
        if self._testMethodName=='test_native_cutout_adapter_checkpoints_outputs_and_releases_scope':smoke_operation='image.remove_background'
        declare(self.store,plan,{'route':'single','operations':[smoke_operation],'goal':'adjust brightness','estimated_seconds':30})
        agent_store.finish(self.store,plan,'succeeded','approved single adjustment')
        self.turn = agent_store.add_turn(self.store, self.project, 'execute', 'edit',plan_id=plan)
        from PIL import Image
        source=self.store.project_dir(self.project)/'sources'/'source.png'
        Image.new('RGB',(8,8),'blue').save(source)
        self.store.add_asset(self.project,'image','source.png','source.png',{'kind':'image','width':8,'height':8})
        self.coordinator = Coordinator(self.store, 'test-node', 'test',ttl=300 if self._testMethodName.startswith('test_real_') else 45)
        self.lease = self.coordinator.claim(self.turn, 'agent')
        self.binding = self.lease.bind();self.binding.__enter__()
        self.addCleanup(self.binding.__exit__,None,None,None)
        self.store.execute("UPDATE agent_turns SET status='running' WHERE id=?",(self.turn,))
        self.pool = IdentityPool(self.root/'node', start=450000, size=4)
        self.runtime = TaskRuntime(self.lease, self.pool)
        self.lease.runtime = self.runtime
        self.env = agent_sandbox.prepare(self.store, self.project, 'test', 'execute', self.turn)
        self.sock = socket.socket(socket.AF_UNIX)
        self.socket_path = self.runtime.path/'gateway.sock'
        self.sock.bind(str(self.socket_path))
        os.chown(self.socket_path,self.runtime.uid,self.runtime.gid)
        self.socket_path.chmod(0o600)

    def tearDown(self):
        self.sock.close()
        try:self.runtime.close()
        finally:self.lease.release()

    def run_script(self, script):
        return subprocess.run(agent_sandbox.command(self.env,self.socket_path,
            ['/opt/venv/bin/python','-c',script]),capture_output=True,text=True,timeout=20)

    def test_unique_identity_seccomp_and_checkpoint(self):
        result = self.run_script('''import os,ctypes
from pathlib import Path
assert os.getuid() >= 450000
assert 'NoNewPrivs:\\t1' in Path('/proc/self/status').read_text()
assert 'Seccomp:\\t2' in Path('/proc/self/status').read_text()
assert ctypes.CDLL(None).unshare(0x10000000) == -1
Path('edit/result.txt').write_text('checkpoint')
print(os.getuid())
''')
        self.assertEqual(result.returncode,0,result.stderr)
        self.assertEqual(int(result.stdout),self.runtime.uid)
        self.runtime.checkpoint()
        row = self.store.query('SELECT * FROM execution_checkpoints')[0]
        self.assertEqual((self.store.root/row['path']/'workspace/edit/result.txt').read_text(),'checkpoint')
        self.assertIn('edit/result.txt',json.loads(row['manifest'])['workspace'])
        self.assertTrue(all(not (p/'cgroup.procs').read_text().strip() for p in self.runtime.groups.paths))

    def test_tmp_storage_has_a_hard_limit(self):
        result = self.run_script('''from pathlib import Path
try:
 with Path('edit/large').open('wb') as f:
  for _ in range(80):f.write(b'x'*1024*1024)
except OSError as e:
 assert e.errno == 28
else:raise AssertionError('scratch allocation exceeded')
''')
        self.assertEqual(result.returncode,0,result.stderr)

    def test_public_runtime_hides_private_skill_code_and_host_tools(self):
        result=self.run_script('''from pathlib import Path
import socket,os,subprocess
assert b'codex' in subprocess.check_output(['/opt/codex','--version'],timeout=10).lower()
assert Path('/opt/video-use/SKILL.md').is_file()
import json,errno
capabilities=Path('/workspace/capabilities.json')
original=capabilities.read_text()
catalog=json.loads(original)
assert any(op['id']=='media.edit' for op in catalog['operations'])
try:capabilities.write_text('{}')
except OSError as exc:assert exc.errno in (errno.EROFS,errno.EACCES)
else:raise AssertionError('Capability catalog must be read-only')
assert capabilities.read_text()==original
assert subprocess.check_output(['/usr/bin/file','--mime-type','-b','/workspace/sources/source.png'],text=True).strip()=='image/png'
assert not Path('/opt/video-use/webapp/agent_host.py').exists()
assert not Path('/usr/bin/ssh').exists()
assert not Path('/root').exists()
assert not Path('/data1/wujing03/video-use/SKILL.md').exists()
try:socket.create_connection(('169.254.169.254',80),timeout=.1)
except OSError:pass
else:raise AssertionError('Unexpected network access')
''')
        self.assertEqual(result.returncode,0,result.stderr)

    def test_fixed_quality_checker_rejects_valid_image_with_wrong_dimensions(self):
        from PIL import Image
        from webapp.workflow_runtime import Adapter
        target=self.runtime.workspace/'edit/candidate.png'
        Image.new('RGB',(8,8),'blue').save(target)
        os.chown(target,self.runtime.uid,self.runtime.gid)
        adapter=Adapter(self.store,'test',self.runtime.path)
        workflow={'project':self.project,'turn_id':self.turn}
        report=adapter.validate(workflow,{'spec':{'kind':'script','acceptance':[
            {'kind':'image','file':'candidate.png','width':16},
            {'kind':'review','criterion':'主体完整，风格符合用户确认的方案'}]}},['edit/candidate.png'])
        self.assertTrue(report['technical_valid'])
        self.assertFalse(report['passed'])
        self.assertTrue(report['requires_review'])
        self.assertEqual(len(report['files'][0]['sha256']),64)

    def test_mkv_is_validated_and_published_as_real_media(self):
        import json
        from webapp.workflow_runtime import Adapter
        from webapp.agent_sandbox import export_results
        target=self.runtime.workspace/'edit/candidate.mkv'
        subprocess.run(['ffmpeg','-v','error','-y','-f','lavfi','-i','color=red:s=160x120:r=10:d=1',
                        '-f','lavfi','-i','sine=frequency=440:sample_rate=48000:duration=1',
                        '-c:v','libx264','-threads','1','-c:a','pcm_s16le','-shortest',str(target)],check=True,capture_output=True)
        os.chown(target,self.runtime.uid,self.runtime.gid)
        adapter=Adapter(self.store,'test',self.runtime.path)
        workflow={'project':self.project,'turn_id':self.turn}
        step={'spec':{'kind':'script','acceptance':[{'kind':'media','file':'candidate.mkv','width':160,'height':120,
             'audio_track_present':True,'video':True,'fps':10,'min_duration':.95,'max_duration':1.1,'decode':True}]}}
        report=adapter.validate(workflow,step,['edit/candidate.mkv'])
        self.assertTrue(report['passed'],report);self.assertEqual(report['files'][0]['kind'],'media')
        artifacts=export_results(self.runtime.env,self.turn)
        exported=next(a for a in artifacts if a['name']=='candidate.mkv')
        self.assertTrue(exported['type'].startswith('video/'))
        target.write_text('not a Matroska file')
        self.assertFalse(adapter.validate(workflow,step,['edit/candidate.mkv'])['technical_valid'])

    def test_real_codex_app_server_initializes_in_the_curated_runtime(self):
        result=self.run_script('''import subprocess,json,selectors
p=subprocess.Popen(['/opt/codex','app-server','--listen','stdio://'],stdin=subprocess.PIPE,stdout=subprocess.PIPE,text=True)
try:
 p.stdin.write(json.dumps({'id':1,'method':'initialize','params':{'clientInfo':{'name':'isolation-test','version':'1'}}})+'\\n');p.stdin.flush()
 with selectors.DefaultSelector() as selector:
  selector.register(p.stdout,selectors.EVENT_READ)
  assert selector.select(8),'Codex initialization timed out'
 reply=json.loads(p.stdout.readline())
 assert reply.get('id')==1 and 'result' in reply,reply
finally:
 p.terminate();p.wait(timeout=3)
''')
        self.assertEqual(result.returncode,0,result.stderr)

    def test_checkpoint_retention_deletion_protection_and_explicit_rollback(self):
        from webapp import checkpoints
        from webapp.task_lifecycle import administrative
        path=self.runtime.workspace/'edit/keep.txt';path.write_text('first')
        self.runtime.checkpoint()
        first=checkpoints.versions(self.store,self.project)[0]['id']
        path.write_text('second');self.runtime.checkpoint()
        path.unlink();self.runtime.checkpoint()
        versions=checkpoints.versions(self.store,self.project)
        self.assertEqual(len(versions),3)
        self.assertIn('edit/keep.txt',versions[0]['changes']['blocked_deletions'])
        current=self.store.query('SELECT path FROM execution_checkpoints')[0]['path']
        self.assertEqual((self.store.root/current/'workspace/edit/keep.txt').read_text(),'second')
        self.store.execute("UPDATE agent_turns SET status='succeeded' WHERE id=?",(self.turn,))
        with self.assertRaisesRegex(ValueError,'回收'):checkpoints.rollback(self.store,self.project,first)
        self.runtime.close();self.lease.release()
        with administrative():
            checkpoints.rollback(self.store,self.project,first)
            current=self.store.query('SELECT path FROM execution_checkpoints')[0]['path']
        self.assertEqual((self.store.root/current/'workspace/edit/keep.txt').read_text(),'first')

    def test_independent_watchdog_stops_task_with_database_unavailable(self):
        import time
        from webapp.execution_reaper import Reaper
        from webapp.task_lifecycle import administrative
        with administrative():reaper=Reaper(self.store,self.pool)
        process=subprocess.Popen(agent_sandbox.command(self.env,self.socket_path,
            ['/opt/venv/bin/python','-c','import time; time.sleep(600)']),stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
        deadline=time.monotonic()+5
        while time.monotonic()<deadline and not (self.runtime.groups.paths[0]/'cgroup.procs').read_text().strip():time.sleep(.02)
        self.runtime.identity.save(permit_until=0)
        try:
            with patch.object(self.store,'connect',side_effect=OSError('DB offline')):
                try:reaper.tick()
                except OSError:pass
                process.wait(timeout=10)
            self.assertNotEqual(process.returncode,0)
            self.assertTrue((self.runtime.path/'revoked').exists())
            # The original live owner still holds the identity lock.
            self.assertTrue((self.pool.root/'records'/f'{self.runtime.uid}.json').exists())
            for child,_,_ in reaper.children.values():child.wait(timeout=10)
        finally:
            if process.poll() is None:process.kill();process.wait()

    def test_foreign_uid_remains_inaccessible_even_if_mounted_by_mistake(self):
        foreign=self.root/'foreign';foreign.mkdir(mode=0o700)
        (foreign/'secret').write_text('other task')
        os.chown(foreign,470000,470000);os.chown(foreign/'secret',470000,470000)
        invocation=agent_sandbox.command(self.env,self.socket_path,['/opt/venv/bin/python','-c',
            "from pathlib import Path\ntry:Path('/foreign/secret').read_text()\nexcept PermissionError:pass\nelse:raise AssertionError('cross-UID access')"])
        position=invocation.index('--chdir')
        invocation[position:position]=['--ro-bind',str(foreign),'/foreign']
        result=subprocess.run(invocation,capture_output=True,text=True,timeout=15)
        self.assertEqual(result.returncode,0,result.stderr)

    def test_image_tool_runs_in_separate_restricted_namespace(self):
        from webapp.agent_gateway import Gateway
        gateway=Gateway(self.runtime.path/'tool-gateway.sock',self.env['project'],self.env['assets'],
            'execute','test','','',self.runtime.uid,self.runtime.gid,lambda *_:None,lambda *_:None,self.env['workspace'])
        handler=Mock()
        try:
            gateway.start()
            gateway.image_edit(handler,{'action':'local_adjust','asset':'image','parameters':{'brightness':1.2}})
            self.assertEqual(handler.reply.call_args.args[0],200,handler.reply.call_args)
            self.assertTrue(handler.reply.call_args.args[1]['files'])
        finally:gateway.close()

    def test_gateway_http_runs_without_root_and_uses_scoped_broker(self):
        import http.client
        import pwd
        from webapp.agent_gateway import Gateway
        gateway=Gateway(self.runtime.path/'http-gateway.sock',self.env['project'],self.env['assets'],
            'execute','test','','',self.runtime.uid,self.runtime.gid,lambda *_:None,lambda *_:None,self.env['workspace'])
        try:
            gateway.start()
            pid=gateway.gateway_process.process.pid
            import time
            deadline=time.monotonic()+5
            while time.monotonic()<deadline:
                status=Path(f'/proc/{pid}/status').read_text()
                if f'Uid:\t{pwd.getpwnam("video-use").pw_uid}\t' in status:break
                time.sleep(.02)
            self.assertIn(f'Uid:\t{pwd.getpwnam("video-use").pw_uid}\t',status)
            connection=http.client.HTTPConnection('localhost',timeout=15)
            connection.sock=socket.socket(socket.AF_UNIX);connection.sock.settimeout(15)
            connection.sock.connect(str(gateway.socket_path))
            connection.request('POST','/image-tools',json.dumps({'action':'local_adjust','asset':'image','parameters':{'brightness':1.1}}),{'Content-Type':'application/json'})
            response=connection.getresponse();body=response.read();connection.close()
            self.assertEqual(response.status,200,body)
            self.assertTrue(json.loads(body)['files'])
        finally:gateway.close()


    def test_review_sandbox_only_writes_new_repair_directory(self):
        self.runtime.env=None
        self.env=agent_sandbox.prepare(self.store,self.project,'test','plan',self.turn,preview_id='a'*32)
        (self.env['workspace']/'edit/finished.txt').write_text('accepted')
        script=f"""from pathlib import Path
for name in ['edit/finished.txt','edit/project.md','edit/new-unreviewed.txt']:
    try:Path(name).write_text('changed')
    except OSError:pass
    else:raise AssertionError('Review changed protected result')
Path({self.env['repair_path']!r},'fix.py').write_text('print(42)')
"""
        result=self.run_script(script)
        self.assertEqual(result.returncode,0,result.stderr)
        self.runtime.checkpoint()
        checkpoint=self.store.query('SELECT path FROM execution_checkpoints WHERE project=?',(self.project,))[0]['path']
        self.assertEqual((self.store.root/checkpoint/'workspace/edit/repairs'/('a'*32)/'fix.py').read_text(),'print(42)')
        self.assertEqual((self.env['workspace']/'edit/finished.txt').read_text(),'accepted')

    def test_unprivileged_gateway_routes_validate_and_scoped_decisions(self):
        import http.client,uuid
        from webapp.agent_gateway import Gateway
        from webapp import workflows,workflow_review
        from tests.test_workflows import local_step
        plan=uuid.uuid4().hex
        self.store.execute("INSERT INTO agent_turns(id,project,mode,prompt,text,status,created,updated) VALUES (?,?,'plan','plan','approved','succeeded',?,?)",(plan,self.project,self.store.now(),self.store.now()))
        self.store.execute('UPDATE agent_turns SET plan_id=? WHERE id=?',(plan,self.turn))
        definition={'key':'gateway','name':'gateway','steps':[local_step()]}
        self.store.execute('DELETE FROM execution_intents')  # Legacy-plan compatibility fixture.
        workflows.submit(self.store,self.turn,definition)
        w=workflows.get(self.store,self.turn)
        files=['edit/workflows/example/final.png']
        workflows.change(self.store,w['id'],'package',candidate=files,quality={'technical_valid':True,'passed':True,'files':[]})
        workflow_review.queue(self.store,workflows.get(self.store,self.turn),w['steps'][0],'review')
        request=workflow_review.begin(self.store,self.turn)
        gateway=Gateway(self.runtime.path/'review.sock',self.env['project'],self.env['assets'],
            'plan','test','','',self.runtime.uid,self.runtime.gid,lambda *_:None,lambda *_:None,self.env['workspace'],
            workflow_callback=lambda body:workflow_review.dispatch(self.store,self.turn,body,'plan',request['id']))
        gateway.workflow_review_id=request['id']
        gateway.start()
        def post(body):
            connection=http.client.HTTPConnection('localhost')
            connection.sock=socket.socket(socket.AF_UNIX);connection.sock.settimeout(15)
            connection.sock.connect(str(gateway.socket_path))
            connection.request('POST','/workflow',json.dumps(body),{'Content-Type':'application/json'})
            response=connection.getresponse();data=json.loads(response.read());status=response.status;connection.close()
            return status,data
        try:
            self.assertEqual(post({'action':'validate','plan':definition})[0],200)
            bad={'key':'bad','name':'bad','steps':[{**local_step(),'acceptance':[]}]}
            self.assertEqual(post({'action':'validate','plan':bad})[0],422)
            decision={'request_id':'wrong','action':'pass','reason':'checked','evidence':[{'step_id':'package','files':files,'observation':'good'}]}
            self.assertEqual(post({'action':'decision','decision':decision})[0],422)
            decision['request_id']=request['id']
            status,data=post({'action':'decision','decision':decision})
            self.assertEqual(status,200,data)
            self.assertEqual(workflow_review.current(self.store,w['id'])['status'],'staged')
            self.assertEqual(post({'action':'submit','plan':definition})[0],403)
        finally:gateway.close()

    def test_native_cutout_adapter_checkpoints_outputs_and_releases_scope(self):
        import contextlib
        from webapp.workflow_runtime import Adapter
        adapter=Adapter(self.store,'test',self.runtime.path)
        def fake_command(env,path,work,gpu,scope,timeout):
            relative=str(work.relative_to(env['workspace']))
            code=f"""from pathlib import Path
from PIL import Image
import json
out=Path({relative!r})/'output'
Image.new('RGBA',(8,8),(255,0,0,128)).save(out/'foreground.png')
Image.new('L',(8,8),128).save(out/'mask.png')
(out/'provider-result.json').write_text(json.dumps(['foreground.png','mask.png']))
"""
            return agent_sandbox.command(dict(env,mode='plan',step_output=relative,resource_scope=scope),path,['python','-c',code])
        from webapp import workflows
        workflows.submit(self.store,self.turn,{'key':'cutout','name':'cutout','steps':[{
            'id':'cutout','kind':'operation','operation':'image.remove_background','inputs':{'source':{'asset':'image'}},
            'parameters':{},'name':'Cutout','timeout':30,'goal':'cutout','on_failure':'pause',
            'acceptance':[{'kind':'image','width':8}]}]})
        w=workflows.get(self.store,self.turn);step=w['steps'][0]
        with patch('webapp.image_sandbox.gpu_slot',return_value=contextlib.nullcontext(0)),patch('webapp.image_sandbox.image_command',side_effect=fake_command):
            files=adapter.fixed_image(w,step,lambda:False)
        self.assertEqual({Path(f).name for f in files},{'foreground.png','mask.png'})
        checkpoint=self.store.query('SELECT path FROM execution_checkpoints WHERE project=?',(self.project,))[0]['path']
        self.assertTrue(all((self.store.root/checkpoint/'workspace'/f).is_file() for f in files))

    def test_operation_uses_real_fixed_image_worker_and_role_contract(self):
        from webapp import workflows
        from webapp.workflow_runtime import Adapter
        from webapp.operations import get
        definition={'key':'op','name':'Operation','steps':[{
            'id':'adjust','name':'Adjust','kind':'operation','operation':'image.local_adjust',
            'inputs':{'source':{'asset':'image'}},'parameters':{'parameters':{'brightness':1.1}},
            'goal':'Adjust brightness','on_failure':'diagnose','acceptance':[{'kind':'image','width':8}]}]}
        self.store.execute('DELETE FROM execution_intents')  # Legacy-plan compatibility fixture.
        workflows.submit(self.store,self.turn,definition)
        w=workflows.get(self.store,self.turn);step=w['steps'][0]
        adapter=Adapter(self.store,'test',self.runtime.path)
        files=adapter.fixed_image(w,step,lambda:False)
        report=adapter.validate(w,step,files)
        self.assertTrue(report['passed'],report)
        from webapp.workflow_inputs import roles
        self.assertEqual(set(roles(step['spec'],files)),{'primary_image'})

    def test_script_preflight_reports_target_environment_and_binds_code(self):
        from webapp import workflows
        from webapp.workflow_runtime import Adapter,StepFailure
        from tests.test_workflows import local_step
        path=self.env['workspace']/'edit/package.py'
        path.write_text('import no_such_package_for_video_use\n')
        os.chown(path,self.runtime.uid,self.runtime.gid)
        self.store.execute('DELETE FROM execution_intents')  # Legacy-plan compatibility fixture.
        workflows.submit(self.store,self.turn,{'key':'script','name':'script','steps':[local_step()]})
        w=workflows.get(self.store,self.turn);adapter=Adapter(self.store,'test',self.runtime.path)
        with self.assertRaises(StepFailure) as caught:adapter.preflight(w,w['steps'][0])
        self.assertIn('no_such_package',json.dumps(caught.exception.diagnostic))
        path.write_text('import json\nprint(1)\n')
        self.assertTrue(adapter.preflight(w,w['steps'][0])['passed'])
        path.write_text('import json\nprint(2)\n')
        w=workflows.get(self.store,self.turn)
        with self.assertRaises(StepFailure):adapter.preflight(w,w['steps'][0])

    @unittest.skipUnless(os.environ.get('VIDEO_USE_TEST_REAL_MODELS')=='1','Explicit real GPU smoke')
    def test_real_birefnet_cutout(self):
        from webapp import workflows
        from webapp.workflow_runtime import Adapter
        from PIL import Image,ImageDraw
        image=Image.new('RGB',(256,256),'white');draw=ImageDraw.Draw(image)
        draw.rounded_rectangle((50,50,206,206),radius=20,fill='#1765c2');draw.ellipse((90,90,166,166),fill='yellow')
        image.save(self.env['sources']/'source.png')
        self.env['assets'][0]['metadata'].update(width=256,height=256)
        self.store.execute('UPDATE assets SET metadata=? WHERE id=?',(json.dumps(self.env['assets'][0]['metadata']),'image'))
        definition={'key':'real-cutout','name':'Real cutout','steps':[{'id':'cutout','name':'cutout','kind':'operation',
            'operation':'image.remove_background','inputs':{'source':{'asset':'image'}},'goal':'Extract logo',
            'on_failure':'pause','timeout':180,'acceptance':[{'kind':'image','file':'foreground.png','has_alpha':True},{'kind':'image','file':'mask.png','width':256}]}]}
        self.store.execute('DELETE FROM execution_intents')  # Legacy-plan compatibility fixture.
        workflows.submit(self.store,self.turn,definition)
        w=workflows.get(self.store,self.turn);adapter=Adapter(self.store,'test',self.runtime.path)
        try:files=adapter.fixed_image(w,w['steps'][0],lambda:False)
        except Exception as exc:
            self.fail(json.dumps(getattr(exc,'diagnostic',{'error':str(exc)}),ensure_ascii=False))
        self.assertTrue(adapter.validate(w,w['steps'][0],files)['passed'])

    def real_image_operation(self,operation,parameters,expected_error=None):
        from webapp import workflows
        from webapp.workflow_runtime import Adapter
        from PIL import Image,ImageDraw
        source=Image.new('RGB',(256,256),'white');draw=ImageDraw.Draw(source)
        draw.rectangle((50,50,200,200),fill='blue');draw.text((70,110),'HELLO VIDEO',fill='white')
        source.save(self.env['sources']/'source.png')
        self.env['assets'][0]['metadata'].update(width=256,height=256)
        self.store.execute('UPDATE assets SET metadata=? WHERE id=?',(json.dumps(self.env['assets'][0]['metadata']),'image'))
        self.store.execute('DELETE FROM execution_intents')
        spec={'id':'model','name':operation,'kind':'operation','operation':operation,'inputs':{'source':{'asset':'image'}},
            'parameters':parameters,'goal':'Functional model smoke','on_failure':'pause','timeout':180,
            'basic_reason':'Smoke checks output validity, not semantic accuracy','acceptance':[{'kind':'file'}]}
        workflows.submit(self.store,self.turn,{'key':'model-smoke','name':'model-smoke','steps':[spec]})
        w=workflows.get(self.store,self.turn);adapter=Adapter(self.store,'test',self.runtime.path)
        if expected_error:
            from webapp.workflow_runtime import StepFailure
            from webapp.execution_contracts import classify_error
            with self.assertRaises(StepFailure) as caught:
                adapter.fixed_image(w,w['steps'][0],lambda:False)
            self.assertEqual(classify_error(caught.exception.diagnostic,'local'),expected_error,
                             caught.exception.diagnostic)
            return
        try:files=adapter.fixed_image(w,w['steps'][0],lambda:False)
        except Exception as exc:self.fail(json.dumps(getattr(exc,'diagnostic',{'error':str(exc)}),ensure_ascii=False))
        self.assertTrue(adapter.validate(w,w['steps'][0],files)['passed'])

    @unittest.skipUnless(os.environ.get('VIDEO_USE_TEST_REAL_MODELS')=='1','Explicit real GPU smoke')
    def test_real_sam2_segment(self):
        self.real_image_operation('image.segment',{'box':[40,40,170,170]})

    @unittest.skipUnless(os.environ.get('VIDEO_USE_TEST_REAL_MODELS')=='1','Explicit real OCR smoke')
    def test_real_ocr(self):
        self.real_image_operation('image.ocr',{})

    @unittest.skipUnless(os.environ.get('VIDEO_USE_TEST_REAL_MODELS')=='1','Explicit real SAM3 smoke')
    def test_real_sam3_segment(self):
        self.real_image_operation('image.segment_sam3',{'box':[40,40,170,170],'parameters':{'refine':True}})

    @unittest.skipUnless(os.environ.get('VIDEO_USE_TEST_REAL_MODELS')=='1','Explicit real SAM3 smoke')
    def test_real_sam3_empty_selection(self):
        # The synthetic input is a blue rectangle with text, not an elephant.
        self.real_image_operation('image.segment_sam3',
            {'prompt':'elephant','parameters':{'refine':False,'confidence':.99}},
            expected_error='model_result')

    def test_registered_media_operation_reuses_worker_in_real_sandbox(self):
        from webapp import workflows
        from webapp.workflow_runtime import Adapter
        source=self.env['sources']/'clip.mp4'
        subprocess.run(['ffmpeg','-v','error','-f','lavfi','-i','testsrc2=size=96x96:rate=5','-t','1','-c:v','libx264','-pix_fmt','yuv420p',str(source)],check=True)
        os.chown(source,self.runtime.uid,self.runtime.gid);source.chmod(0o600)
        asset={'id':'clip','filename':'clip.mp4','name':'clip','metadata':{'kind':'video','width':96,'height':96,'duration':1,'audio_tracks':[]}}
        self.env['assets'].append(asset)
        self.store.add_asset(self.project,'clip','clip','clip.mp4',asset['metadata'])
        self.store.execute('DELETE FROM execution_intents')
        spec={'id':'timeline','name':'Timeline','kind':'operation','operation':'media.timeline','inputs':{'source':{'asset':'clip'}},
            'parameters':{'start':0,'end':1,'frames':2},'goal':'Inspect clip','on_failure':'pause','basic_reason':'Functional image generation','acceptance':[{'kind':'image'}]}
        workflows.submit(self.store,self.turn,{'key':'media','name':'media','steps':[spec]})
        w=workflows.get(self.store,self.turn);adapter=Adapter(self.store,'test',self.runtime.path)
        try:files=adapter.fixed_media(w,w['steps'][0],lambda:False)
        except Exception as exc:self.fail(json.dumps(getattr(exc,'diagnostic',{'error':str(exc)}),ensure_ascii=False))
        self.assertTrue(adapter.validate(w,w['steps'][0],files)['passed'])

    def test_transcription_cache_is_bound_to_audio_and_parameters(self):
        import wave,math,struct,sys
        from webapp import workflows
        from webapp.workflow_runtime import Adapter
        source=self.env['sources']/'speech.wav'
        with wave.open(str(source),'wb') as wav:
            wav.setnchannels(1);wav.setsampwidth(2);wav.setframerate(16000)
            wav.writeframes(b''.join(struct.pack('<h',int(8000*math.sin(i*.15))) for i in range(16000)))
        os.chown(source,self.runtime.uid,self.runtime.gid);source.chmod(0o600)
        asset={'id':'speech','filename':'speech.wav','name':'speech','metadata':{'kind':'audio','duration':1,'audio_tracks':[{}]}}
        self.env['assets'].append(asset);self.store.add_asset(self.project,'speech','speech','speech.wav',asset['metadata'])
        self.store.execute('DELETE FROM execution_intents')
        spec={'id':'first','name':'Transcribe','kind':'operation','operation':'audio.transcribe','inputs':{'source':{'asset':'speech'}},
            'goal':'Recognize speech','on_failure':'pause','acceptance':[{'kind':'json','required_keys':['text']}]}
        helpers=Path(__file__).resolve().parents[1]/'helpers'
        if str(helpers) not in sys.path:sys.path.insert(0,str(helpers))
        with patch.dict(os.environ,{'ELEVENLABS_API_KEY':'test-only-key'}):
            workflows.submit(self.store,self.turn,{'key':'speech','name':'speech','steps':[spec,{**spec,'id':'second','depends_on':['first']}]})
        adapter=Adapter(self.store,'test',self.runtime.path);w=workflows.get(self.store,self.turn)
        with patch('transcribe.call_scribe',return_value={'text':'hello','words':[]}) as call:
            first=adapter.transcribe(w,w['steps'][0],lambda:False)
            second=adapter.transcribe(workflows.get(self.store,self.turn),w['steps'][1],lambda:False)
            self.assertEqual(call.call_count,1)
        self.assertNotEqual(first,second)
        self.assertEqual((self.env['workspace']/first[0]).read_bytes(),(self.env['workspace']/second[0]).read_bytes())
        self.assertEqual(len(self.store.query('SELECT * FROM workflow_provider_calls')),1)

    def test_preflight_command_uses_the_real_parent_broker(self):
        import http.client
        from webapp.agent_gateway import Gateway
        from webapp.workflow_review import dispatch
        from tests.test_workflows import local_step
        script=self.env['workspace']/'edit/package.py';script.write_text('import json\nprint(1)\n')
        os.chown(script,self.runtime.uid,self.runtime.gid);script.chmod(0o600)
        gateway=Gateway(self.runtime.path/'preflight.sock',self.env['project'],self.env['assets'],'execute','test','','',
            self.runtime.uid,self.runtime.gid,lambda *_:None,lambda *_:None,self.env['workspace'],
            workflow_callback=lambda body:dispatch(self.store,self.turn,body,'execute'))
        gateway.start()
        try:
            client=http.client.HTTPConnection('localhost');client.sock=socket.socket(socket.AF_UNIX);client.sock.settimeout(20);client.sock.connect(str(gateway.socket_path))
            client.request('POST','/workflow',json.dumps({'action':'preflight','plan':{'key':'test','name':'test','steps':[local_step()]}}),{'Content-Type':'application/json'})
            response=client.getresponse();body=json.loads(response.read());client.close()
            self.assertEqual(response.status,200,body);self.assertTrue(body['passed'],body)
            self.assertIn('edit/package.py',body['reports']['package']['files'])
        finally:gateway.close()


if __name__ == '__main__':unittest.main()
