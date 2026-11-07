import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

from webapp import agent_store
from webapp.command_workspace_cleanup import sweep
from webapp.sandbox_backends import OpenSandboxCleanupRegistry
from webapp.store import Store
from webapp.task_leases import Coordinator


class CommandWorkspaceCleanupTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.store=Store(Path(self.temp.name)/'node');agent_store.initialize(self.store)
        project=self.store.add_project('owner','cleanup')['id']
        turn=agent_store.add_turn(self.store,project,'plan','test')
        self.lease=Coordinator(self.store,'node','release').claim(turn,'agent')
        self.addCleanup(self.lease.release)
        self.registry=OpenSandboxCleanupRegistry();self.registry.reserve(self.lease)
        self.path=self.store.root/'command-attempts'/self.lease.attempt
        self.path.mkdir(parents=True);(self.path/'payload').write_bytes(b'input copy')
        self.age()

    def age(self):os.utime(self.path,(time.time()-1000,)*2)

    def test_live_lease_and_unconfirmed_remote_cleanup_are_retained(self):
        self.registry.complete(self.store,self.lease.attempt)
        self.assertEqual(sweep(self.store)['removed'],0);self.assertTrue(self.path.exists())
        self.lease.release()
        self.store.execute("UPDATE execution_external_cleanup SET state='pending'")
        self.age();self.assertEqual(sweep(self.store)['removed'],0)
        self.registry.complete(self.store,self.lease.attempt)
        self.assertEqual(sweep(self.store)['removed'],1);self.assertFalse(self.path.exists())

    def test_killed_local_owner_is_reaped_only_after_remote_cleanup(self):
        script="""import fcntl,os,sys,time
fd=os.open(sys.argv[1],os.O_CREAT|os.O_RDWR,0o600)
fcntl.flock(fd,fcntl.LOCK_EX)
print('locked',flush=True)
time.sleep(60)
"""
        child=subprocess.Popen([sys.executable,'-c',script,str(self.path/'.owner')],stdout=subprocess.PIPE,text=True)
        try:
            import select
            self.assertTrue(select.select([child.stdout],[],[],5)[0]);self.assertEqual(child.stdout.readline().strip(),'locked')
            self.lease.release();self.registry.complete(self.store,self.lease.attempt);self.age()
            self.assertEqual(sweep(self.store)['removed'],0)
            child.kill();child.wait(timeout=5)
            self.assertEqual(sweep(self.store)['removed'],1)
            self.assertFalse(self.path.exists())
        finally:
            if child.poll() is None:child.kill()
            child.wait(timeout=5);child.stdout.close()

    def test_unknown_database_state_and_symlink_are_never_removed(self):
        self.lease.release();self.registry.complete(self.store,self.lease.attempt)
        with patch.object(self.store,'query',side_effect=OSError('database offline')):
            with self.assertRaises(OSError):sweep(self.store)
        self.assertTrue(self.path.exists())
        outside=Path(self.temp.name)/'outside';outside.mkdir();(outside/'keep').write_bytes(b'safe')
        link=self.path.parent/('b'*32);link.symlink_to(outside,target_is_directory=True)
        self.age();sweep(self.store)
        self.assertTrue(link.is_symlink());self.assertEqual((outside/'keep').read_bytes(),b'safe')

    def test_watchdog_launches_cleanup_on_its_own_data_root(self):
        from webapp.execution_reaper import Reaper
        from webapp.execution_resources import IdentityPool
        self.lease.release();self.registry.complete(self.store,self.lease.attempt)
        pool=IdentityPool(Path(self.temp.name)/'registry',start=480000,size=1)
        reaper=Reaper(self.store,pool)
        reaper.clean_command_workspaces();child=reaper.command_cleanup_child
        self.assertIsNotNone(child)
        try:
            child.wait(timeout=10);reaper.clean_command_workspaces()
            self.assertFalse(self.path.exists());self.assertIsNone(reaper.command_cleanup_child)
        finally:
            if child.poll() is None:child.kill()
            child.communicate(timeout=5)
