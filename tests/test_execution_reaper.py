import json
import os
from pathlib import Path
import tempfile
import unittest
import threading
import time
import shutil
import subprocess
import sys
from unittest.mock import patch,Mock
from webapp import agent_store
from webapp.store import Store
from webapp.task_leases import Coordinator
from webapp.execution_resources import IdentityPool
from webapp.execution_reaper import Reaper


@unittest.skipUnless(os.geteuid()==0,'Requires private node registry')
class ReaperStateTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();root=Path(self.temp.name)
        self.store=Store(root/'data');agent_store.initialize(self.store)
        self.pool=IdentityPool(root/'node',start=480000,size=1)
    def tearDown(self):self.temp.cleanup()
    def test_service_capabilities_allow_cleanup_in_live_worker_mount_namespace(self):
        from deploy import cluster
        attempt='c'*32;run=self.pool.root/'runs'/attempt;run.mkdir();(run/'data').mkdir()
        script="""import os,sys,subprocess
from pathlib import Path
target=Path(sys.argv[1])
subprocess.run(['mount','-t','tmpfs','-o','size=1m','tmpfs',str(target)],check=True)
(target/'remaining').write_text('orphan')
print('ready',flush=True)
sys.stdin.readline()
print('mounted' if os.path.ismount(target) else 'clean',flush=True)
"""
        owner=subprocess.Popen(['unshare','--mount','--propagation','private',sys.executable,'-c',script,str(run/'data')],
                               stdin=subprocess.PIPE,stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True)
        try:
            import select
            self.assertTrue(select.select([owner.stdout],[],[],10)[0])
            self.assertEqual(owner.stdout.readline().strip(),'ready')
            record={'attempt':attempt,'uid':480000,'mount_owner':owner.pid,
                    'mount_owner_start':Path(f'/proc/{owner.pid}/stat').read_text().rsplit(')',1)[1].split()[19],
                    'boot_id':Path('/proc/sys/kernel/random/boot_id').read_text().strip()}
            (self.pool.root/'records/480000.json').write_text(json.dumps(record))
            (self.pool.root/'identities/480000').write_text(json.dumps(record))
            capabilities=next(line.split('=',1)[1] for line in cluster.reaper_unit().splitlines() if line.startswith('CapabilityBoundingSet='))
            bounded='-all,'+','.join('+'+cap.removeprefix('CAP_').lower() for cap in capabilities.split())
            env={**os.environ,'VIDEO_USE_EXECUTOR_ROOT':str(self.pool.root),'VIDEO_USE_UID_START':'480000','VIDEO_USE_UID_COUNT':'1'}
            command=['setpriv','--bounding-set='+bounded,sys.executable,'-m','webapp.execution_reaper','clean','480000',attempt]
            result=subprocess.run(command,env=env,capture_output=True,text=True,timeout=15)
            self.assertEqual(result.returncode,0,result.stderr)
            self.assertFalse(run.exists());self.assertFalse((self.pool.root/'records/480000.json').exists())
            self.assertEqual((self.pool.root/'identities/480000').stat().st_size,0)
            output,_=owner.communicate('\n',timeout=5)
            self.assertEqual(output.strip(),'clean')
        finally:
            if owner.poll() is None:owner.kill();owner.communicate(timeout=5)

    def test_watchdog_revocation_cannot_recreate_files_during_teardown(self):
        attempt='b'*32;run=self.pool.root/'runs'/attempt;run.mkdir()
        (run/'payload').write_text('completed')
        record={'attempt':attempt,'uid':480000,'boot_id':'previous-boot'}
        reaper=Reaper(self.store,self.pool)
        deleting=threading.Event();release=threading.Event();revoking=threading.Event()
        errors=[];original=shutil.rmtree
        def slow_delete(path,*args,**kwargs):
            deleting.set();self.assertTrue(release.wait(5))
            return original(path,*args,**kwargs)
        def guarded(fn):
            try:fn()
            except Exception as exc:errors.append(exc)
        def revoke():
            revoking.set();reaper.launch(record)
        with patch('webapp.execution_resources.shutil.rmtree',side_effect=slow_delete),patch('webapp.execution_reaper.subprocess.Popen'):
            cleaner=threading.Thread(target=lambda:guarded(lambda:self.pool.cleanup(record,480000)))
            watchdog=threading.Thread(target=lambda:guarded(revoke))
            cleaner.start();self.assertTrue(deleting.wait(5))
            watchdog.start();self.assertTrue(revoking.wait(5))
            try:
                time.sleep(.05)
                self.assertFalse((run/'revoked').exists())
            finally:
                release.set();cleaner.join(5);watchdog.join(5)
        self.assertFalse(cleaner.is_alive());self.assertFalse(watchdog.is_alive())
        self.assertEqual(errors,[]);self.assertFalse(run.exists())

    def test_transient_cleanup_failure_retries_before_quarantining_node(self):
        project=self.store.add_project('owner','project')['id']
        turn=agent_store.add_turn(self.store,project,'plan','test')
        lease=Coordinator(self.store,self.pool.node,'test').claim(turn,'agent');lease.release()
        self.store.execute('UPDATE execution_attempts SET uid=480000 WHERE id=?',(lease.attempt,))
        record={'attempt':lease.attempt,'uid':480000,'task_id':turn,
                'boot_id':Path('/proc/sys/kernel/random/boot_id').read_text().strip(),'permit_until':time.monotonic()+120}
        (self.pool.root/'records/480000.json').write_text(json.dumps(record))
        (self.pool.root/'runs'/lease.attempt).mkdir()
        self.store.execute("INSERT INTO execution_cleanup(attempt,task_id,node,state,updated,deadline_at) VALUES (?,?,?,'cleaning',?,?)",
                           (lease.attempt,turn,self.pool.node,self.store.now(),self.store.now()+60))
        reaper=Reaper(self.store,self.pool)
        reaper.children[lease.attempt]=(Mock(poll=lambda:1),time.monotonic(),record)
        reaper.tick()
        self.assertEqual(self.store.query('SELECT state FROM execution_cleanup')[0]['state'],'cleaning')
        self.assertFalse((self.pool.root/'node-quarantined').exists())
        self.store.execute('UPDATE execution_cleanup SET deadline_at=?,next_retry=?',(self.store.now()-1,self.store.now()+60))
        reaper.tick()
        self.assertTrue((self.pool.root/'node-quarantined').exists())
        self.assertEqual(self.store.query('SELECT state FROM execution_cleanup')[0]['state'],'quarantined')

    def test_startup_does_not_require_available_database(self):
        with patch.object(self.store,'connect',side_effect=OSError('DB offline')):
            reaper=Reaper(self.store,self.pool)
        self.assertEqual(reaper.pool,self.pool)
    def test_reused_identity_does_not_keep_an_old_cleaned_attempt_reserved(self):
        project=self.store.add_project('owner','project')['id']
        turn=agent_store.add_turn(self.store,project,'plan','test')
        lease=Coordinator(self.store,self.pool.node,'test').claim(turn,'agent')
        self.store.execute('UPDATE execution_attempts SET uid=480000 WHERE id=?',(lease.attempt,))
        lease.release()
        self.store.execute("INSERT INTO execution_cleanup(attempt,task_id,node,state,updated) VALUES (?,?,?,'cleaning',?)",(lease.attempt,turn,self.pool.node,self.store.now()))
        (self.pool.root/'records/480000.json').write_text(json.dumps({'attempt':'a'*32,'uid':480000}))
        Reaper(self.store,self.pool).tick()
        self.assertEqual(self.store.query('SELECT state FROM execution_cleanup')[0]['state'],'complete')
        self.assertTrue((self.pool.root/'records/480000.json').exists())

    def test_late_old_cleanup_cannot_revoke_a_new_attempt_of_same_task(self):
        project=self.store.add_project('owner','project')['id']
        turn=agent_store.add_turn(self.store,project,'plan','test')
        coordinator=Coordinator(self.store,self.pool.node,'test')
        old=coordinator.claim(turn,'agent');old.release()
        self.store.execute("INSERT INTO execution_cleanup(attempt,task_id,node,state,updated) VALUES (?,?,?,'complete',?)",
                           (old.attempt,turn,self.pool.node,self.store.now()))
        current=coordinator.claim(turn,'agent')
        self.store.execute("INSERT INTO execution_cleanup(attempt,task_id,node,state,updated) VALUES (?,?,?,'allocated',?)",
                           (current.attempt,turn,self.pool.node,self.store.now()))
        Reaper(self.store,self.pool).mark({'attempt':old.attempt,'task_id':turn,'uid':480000},'complete')
        row=self.store.query('SELECT state FROM execution_cleanup WHERE attempt=?',(current.attempt,))[0]
        self.assertEqual(row['state'],'allocated')
        current.check();current.release()

if __name__=='__main__':unittest.main()
