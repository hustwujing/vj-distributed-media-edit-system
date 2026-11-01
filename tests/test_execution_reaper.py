import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
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
