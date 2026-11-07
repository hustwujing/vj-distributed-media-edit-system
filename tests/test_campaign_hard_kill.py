import sys,unittest
from types import SimpleNamespace
from unittest.mock import patch
from tests import campaign_dynamic_probe
with patch.dict(sys.modules,{'campaign_dynamic_probe':campaign_dynamic_probe}):
    from tests.campaign_dynamic_faults import kill_frozen_attempt,frozen_attempt_status


class FrozenKillTests(unittest.TestCase):
    def setUp(self):
        self.calls=[];self.unit='sandbox-agent-executor@a.service'
        self.runtime=SimpleNamespace(WORKERS=[self.unit],run=lambda args,**kw:self.calls.append(args))
        self.row={'unit':self.unit,'id':'attempt','status':'running'}

    def observe(self):
        self.assertIn('--signal=SIGSTOP',self.calls[-1])
        return {'attempts':[self.row]}

    def test_completed_poll_is_resumed_without_kill(self):
        self.row['status']='finished'
        with self.assertRaisesRegex(ValueError,'No replay-safe'):
            kill_frozen_attempt(self.unit,'attempt',self.observe,self.runtime)
        self.assertEqual([call[3] for call in self.calls],['--signal=SIGSTOP','--signal=SIGCONT'])

    def test_live_attempt_is_killed_only_after_frozen_revalidation(self):
        self.assertEqual(kill_frozen_attempt(self.unit,'attempt',self.observe,self.runtime),self.row)
        self.assertIn('--signal=SIGKILL',self.calls[1]);self.assertEqual(self.calls[2],['systemctl','stop',self.unit])

    def test_probe_failure_cannot_leave_worker_stopped(self):
        def broken():raise RuntimeError('probe failed')
        with self.assertRaisesRegex(RuntimeError,'probe failed'):
            kill_frozen_attempt(self.unit,'attempt',broken,self.runtime)
        self.assertIn('--signal=SIGCONT',self.calls[-1])
        self.assertFalse(any('--signal=SIGKILL' in call for call in self.calls))

    def test_foreign_unit_rejected_before_signals(self):
        with self.assertRaises(ValueError):kill_frozen_attempt('source-executor@a.service','attempt',self.observe,self.runtime)
        self.assertEqual(self.calls,[])

    def test_frozen_probe_only_uses_project_scoped_read_query(self):
        def query(sql,args):
            self.assertEqual(args,('attempt','project'))
            self.assertIn('r.project_id=?',sql)
            return [{'id':'attempt','status':'running'}]
        store=SimpleNamespace(query=query)
        self.assertEqual(frozen_attempt_status(store,'project',self.unit,'attempt'),{'attempts':[self.row]})
