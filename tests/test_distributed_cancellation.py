import http.server
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

from tests.distributed_support import PostgresServer
from webapp import agent_store
from webapp.store import Store
from webapp.task_leases import Coordinator
from webapp.cancellation import Monitor, stop_process


class DistributedCancellationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):cls.server = PostgresServer()

    @classmethod
    def tearDownClass(cls):cls.server.close()

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.temp.name)/'worker', dsn=self.server.dsn)
        agent_store.initialize(self.store)
        self.api = Store(Path(self.temp.name)/'api', dsn=self.server.dsn)
        project = self.api.add_project(self.temp.name, 'cancel')['id']
        self.task = agent_store.add_turn(self.api, project, 'plan', 'cancel test')
        self.coordinator = Coordinator(self.store, self.temp.name, 'test', ttl=3)
        self.lease = self.coordinator.claim(self.task, 'agent')
        self.monitor = Monitor(self.store, interval=.05, read_timeout=.25)

    def tearDown(self):
        self.monitor.close()
        try:self.lease.release()
        except Exception:pass
        self.temp.cleanup()

    def cancel(self):
        self.api.execute("UPDATE agent_turns SET status='cancelled' WHERE id=?", (self.task,))

    def test_other_node_cancels_real_process_without_messages(self):
        process = subprocess.Popen([sys.executable,'-c','import time; time.sleep(30)'], start_new_session=True)
        try:
            with self.monitor.register_lease(self.lease,'local') as call:
                call.install(lambda:stop_process(process, .1))
                self.cancel()
                process.wait(timeout=3)
                self.assertNotEqual(process.returncode, 0)
                self.assertTrue(call.cancelled.is_set())
        finally:
            if process.poll() is None:stop_process(process,.1)

    def test_cancel_before_handle_install_is_sticky_and_idempotent(self):
        with self.monitor.register_lease(self.lease,'local') as call:
            self.cancel()
            self.assertTrue(call.cancelled.wait(2))
            invoked = []
            done = threading.Event()
            call.install(lambda:(invoked.append(1),done.set()))
            call.cancel('again')
            self.assertTrue(done.wait(2))
            self.assertEqual(invoked,[1])

    def test_database_observer_block_does_not_extend_local_permit(self):
        started, unblock, stopped = threading.Event(),threading.Event(),threading.Event()
        def blocked(*args,**kwargs):
            started.set();unblock.wait(3)
            raise ConnectionError('partition')
        try:
            with patch.object(self.monitor.store,'query',side_effect=blocked):
                with self.monitor.register(self.task,self.lease.attempt,self.lease.generation,.2,'local') as call:
                    call.install(stopped.set)
                    self.assertTrue(started.wait(1))
                    self.assertTrue(stopped.wait(1))
                    self.assertEqual(call.reason,'permit_expired')
        finally:unblock.set()

    def test_slow_stop_callback_does_not_block_other_cancellations(self):
        unblock, stopped = threading.Event(),threading.Event()
        try:
            with self.monitor.register_lease(self.lease,'local') as first, self.monitor.register_lease(self.lease,'http') as second:
                first.install(lambda:unblock.wait(3))
                second.install(stopped.set)
                self.cancel()
                self.assertTrue(stopped.wait(1))
        finally:unblock.set()

    def test_real_blocked_http_response_is_interrupted(self):
        entered, release, ended = threading.Event(),threading.Event(),threading.Event()
        class Handler(http.server.BaseHTTPRequestHandler):
            def do_GET(self):
                entered.set();release.wait(5)
                try:self.send_response(200);self.end_headers();self.wfile.write(b'ok')
                except OSError:pass
            def log_message(self,*args):pass
        server = http.server.ThreadingHTTPServer(('127.0.0.1',0),Handler)
        thread = threading.Thread(target=server.serve_forever,daemon=True);thread.start()
        outcome=[]
        def request():
            from webapp.egress_policy import session
            try:
                with self.lease.bind(), session() as client:
                    client.get(f'http://127.0.0.1:{server.server_port}',timeout=20)
                outcome.append('unexpected success')
            except Exception:outcome.append('interrupted')
            finally:ended.set()
        caller=threading.Thread(target=request,daemon=True)
        try:
            with patch('webapp.cancellation.for_store',return_value=self.monitor):
                caller.start()
                self.assertTrue(entered.wait(2))
                self.cancel()
                self.assertTrue(ended.wait(2))
                self.assertEqual(outcome,['interrupted'])
        finally:
            release.set();server.shutdown();server.server_close();caller.join(timeout=3)

    def test_real_database_outage_expires_permit_and_stops_process(self):
        process=subprocess.Popen([sys.executable,'-c','import time; time.sleep(30)'],start_new_session=True)
        stopped=False
        try:
            with self.monitor.register_lease(self.lease,'local') as call:
                call.install(lambda:stop_process(process,.1))
                self.server.command('pg_ctl','-D',str(self.server.root/'data'),'-m','immediate','-w','stop')
                stopped=True
                process.wait(timeout=5)
                self.assertTrue(call.cancelled.is_set())
                self.assertEqual(call.reason,'permit_expired')
        finally:
            if process.poll() is None:stop_process(process,.1)
            if stopped:self.server.start()

    def test_late_provider_id_after_cancel_is_saved_only_for_cleanup(self):
        from webapp import provider_calls
        from webapp.task_lifecycle import terminate,TaskLimitReached
        from webapp.task_leases import LeaseLost
        key=self.task+':image:0'
        self.store.execute('''INSERT INTO workflow_provider_calls(
          request_key,workflow,step,revision,attempt,provider,operation,state,inputs,created,updated)
          VALUES (?,?,?,?,?,?,?,?,?,?,?)''',
          (key,self.task,'image',0,1,'http://127.0.0.1:9876','image.edit','submitting','{}',0,0))
        self.cancel()
        with self.api.connect() as db:terminate(db,self.task,'cancelled')
        with self.lease.bind(),self.assertRaises((TaskLimitReached,LeaseLost)):
            provider_calls.update(self.store,{'id':self.task,'run_id':self.task},{'id':'image','revision':0},'waiting','late-job')
        receipt=self.api.query('SELECT external_id FROM workflow_provider_calls WHERE request_key=?',(key,))[0]
        self.assertEqual(receipt['external_id'],'late-job')
        cleanup=self.api.query('SELECT external_id,state FROM execution_external_cleanup WHERE id=?',(key,))[0]
        self.assertEqual(cleanup,{'external_id':'late-job','state':'pending'})
        self.assertEqual(self.api.query('SELECT status FROM agent_turns WHERE id=?',(self.task,))[0]['status'],'cancelled')

    def test_many_calls_share_one_batched_observation_and_bounded_threads(self):
        monitor=Monitor(self.store,interval=10)
        calls=[]
        try:
            for _ in range(200):calls.append(monitor.register(self.task,self.lease.attempt,self.lease.generation,10,'http'))
            original=monitor.store.query
            with patch.object(monitor.store,'query',wraps=original) as query:
                monitor.observe_once();self.assertEqual(query.call_count,1)
                self.assertEqual(len(query.call_args.args[1]),1)
            self.cancel();monitor.observe_once()
            self.assertTrue(all(c.cancelled.is_set() for c in calls))
            self.assertTrue(monitor.observer.is_alive());self.assertTrue(monitor.watchdog.is_alive())
        finally:
            for c in calls:c.close()
            monitor.close()

    def test_unregister_and_old_callback_do_not_stop_new_call(self):
        invoked=[]
        old=self.monitor.register_lease(self.lease,'http');old.install(lambda:invoked.append('old'));old.close()
        with self.monitor.register_lease(self.lease,'http') as current:
            old.cancel('late cancellation')
            self.assertFalse(current.cancelled.is_set());self.assertEqual(invoked,[])
            self.cancel();self.assertTrue(current.cancelled.wait(2))
            done=threading.Event();current.install(done.set);self.assertTrue(done.wait(2))

    def test_database_statement_timeout_does_not_wait_for_control_lock(self):
        # Hold the serialization lock on the API connection; observation has its
        # own read-only connection and must still see the old committed state.
        with self.monitor.register_lease(self.lease,'http'),self.api.connect() as db:
            db.execute("UPDATE agent_turns SET text='uncommitted' WHERE id=?",(self.task,))
            started=time.monotonic();self.monitor.observe_once()
            self.assertLess(time.monotonic()-started,1)

    def test_blocking_object_sdk_is_stopped_in_owned_process(self):
        from webapp.artifact_store import S3Objects
        from webapp.task_lifecycle import cancel_task
        entered,release,finished=threading.Event(),threading.Event(),threading.Event()
        class Handler(http.server.BaseHTTPRequestHandler):
            def log_message(self,*args):pass
            def do_GET(self):
                entered.set();release.wait(8)
                try:self.send_response(503);self.end_headers()
                except OSError:pass
        server=http.server.ThreadingHTTPServer(('127.0.0.1',0),Handler)
        threading.Thread(target=server.serve_forever,daemon=True).start()
        backend=S3Objects('bucket',f'http://127.0.0.1:{server.server_port}',aws_access_key_id='test',aws_secret_access_key='test',region_name='us-east-1')
        outcome=[]
        def download():
            try:
                with self.lease.bind(),backend.get('blocked.bin',max_bytes=10) as stream:stream.read()
                outcome.append('unexpected_success')
            except Exception:outcome.append('stopped')
            finally:finished.set()
        thread=threading.Thread(target=download,daemon=True)
        try:
            with patch('webapp.cancellation.for_store',return_value=self.monitor):
                thread.start();self.assertTrue(entered.wait(3))
                started=time.monotonic();cancel_task(self.api,self.task)
                self.assertTrue(finished.wait(3));self.assertLess(time.monotonic()-started,3)
                self.assertEqual(outcome,['stopped']);self.assertFalse(self.monitor.snapshot())
        finally:release.set();thread.join(timeout=4);server.shutdown();server.server_close()


if __name__=='__main__':unittest.main()
