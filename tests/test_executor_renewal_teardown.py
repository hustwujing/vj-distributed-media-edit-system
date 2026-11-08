"""Deterministic in-flight renewal races across bounded heartbeat shutdown."""
from contextlib import nullcontext
from types import SimpleNamespace
import threading
import unittest
from unittest.mock import Mock,patch

from webapp.agent_host import AgentHost
from webapp.task_leases import LeaseLost


class RenewalTeardownTests(unittest.TestCase):
    def run_race(self,fail_during_work):
        entered=threading.Event();allow_failure=threading.Event();cleaned=threading.Event()
        real_thread=threading.Thread;threads=[]
        class BoundedJoin:
            def __init__(self,**kwargs):
                self.worker=real_thread(**kwargs);self.join_calls=[];threads.append(self)
            def start(self):self.worker.start()
            def join(self,timeout):
                self.join_calls.append(timeout)
                # Model a renewal still blocked after the bounded join returns.
                # Events, not elapsed sleep, determine this race's ordering.
        runtime=SimpleNamespace()
        runtime.begin_cleanup=Mock(side_effect=allow_failure.set)
        runtime.quiesce=Mock(side_effect=cleaned.set)
        runtime.close=Mock(side_effect=lambda:threads[0].worker.join(timeout=2))
        def renew():
            entered.set()
            if not allow_failure.wait(2):raise AssertionError('Renewal race was not released')
            raise LeaseLost('in-flight renewal finished after revocation')
        lease=SimpleNamespace(ttl=.03,bind=nullcontext,renew=renew,runtime=runtime,lost=threading.Event(),
            run_id='run',attempt='attempt',step_id='step',kind='workflow',generation=1,worker='test',
            stop_compute=Mock(),release=Mock())
        host=object.__new__(AgentHost);host.store=object();host.stop=threading.Event()
        host.notify=Mock();host.redact=str
        def execute(*args):
            self.assertTrue(entered.wait(2))
            if fail_during_work:
                allow_failure.set();self.assertTrue(cleaned.wait(2))
        try:
            with patch('webapp.agent_host.threading.Thread',BoundedJoin), \
                 patch('webapp.workflow_graph.get',return_value={'id':'run'}), \
                 patch('webapp.scheduling.requirements',return_value={'gpu':False}), \
                 patch('webapp.workflow_executor.execute',side_effect=execute), \
                 patch('webapp.telemetry.emit') as emit:
                host._execute_claim(lease)
                warnings=[call for call in emit.call_args_list if call.args[0]=='executor.renew_failed']
            self.assertFalse(threads[0].worker.is_alive())
            self.assertEqual(threads[0].join_calls,[3])
            runtime.close.assert_called_once();lease.release.assert_called_once()
            return runtime,lease,warnings
        finally:
            allow_failure.set()
            for thread in threads:thread.worker.join(timeout=2)

    def test_late_renewal_failure_during_teardown_does_not_repeat_cleanup(self):
        runtime,lease,warnings=self.run_race(False)
        runtime.begin_cleanup.assert_called_once()
        runtime.quiesce.assert_not_called()
        self.assertFalse(lease.lost.is_set())
        self.assertEqual(warnings,[])

    def test_renewal_failure_during_work_still_revokes_execution(self):
        runtime,lease,warnings=self.run_race(True)
        runtime.quiesce.assert_called_once()
        self.assertTrue(lease.lost.is_set())
        self.assertEqual(len(warnings),1)
