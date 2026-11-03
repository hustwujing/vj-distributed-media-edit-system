import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import MagicMock,patch
from webapp import agent_store,external_cleanup
from webapp.store import Store


class ExternalCleanupTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.store=Store(Path(self.temp.name))
        agent_store.initialize(self.store)
        self.store.execute("INSERT INTO execution_external_cleanup(id,task_id,provider,external_id,state) VALUES ('one','task','http://127.0.0.1:9898','job123','pending')")
        self.store.execute("INSERT INTO execution_provider_calls VALUES ('one','task','attempt','waiting','job123','http://127.0.0.1:9898')")
    def tearDown(self):self.temp.cleanup()
    def test_cancel_then_poll_confirms_without_generation_resubmission(self):
        client=MagicMock();client.__enter__.return_value=client
        response=client.request.return_value.__enter__.return_value
        response.iter_content.return_value=[json.dumps({'status':'running'}).encode()]
        with patch('webapp.external_cleanup.session',return_value=client):external_cleanup.run(self.store,'one')
        self.assertEqual(client.request.call_args.args[:2],('POST','http://127.0.0.1:9898/api/jobs/job123/cancel'))
        self.store.execute('UPDATE execution_external_cleanup SET next_retry=0')
        response.iter_content.return_value=[b'{"status":"cancelled"}']
        with patch('webapp.external_cleanup.session',return_value=client):external_cleanup.run(self.store,'one')
        self.assertEqual(client.request.call_args.args[:2],('GET','http://127.0.0.1:9898/api/jobs/job123'))
        self.assertEqual(self.store.query('SELECT state FROM execution_external_cleanup')[0]['state'],'complete')
    def test_bounded_failure_and_unknown_submission_never_generate(self):
        client=MagicMock();client.__enter__.return_value=client;client.request.side_effect=OSError('offline')
        with patch('webapp.external_cleanup.session',return_value=client):
            for _ in range(5):
                self.store.execute('UPDATE execution_external_cleanup SET next_retry=0')
                external_cleanup.run(self.store,'one')
        self.assertEqual(client.request.call_count,3)
        self.assertEqual(self.store.query('SELECT state FROM execution_external_cleanup')[0]['state'],'unconfirmed')
        self.store.execute("UPDATE execution_external_cleanup SET state='unknown',attempts=0,external_id=''")
        with patch('webapp.external_cleanup.session') as factory:external_cleanup.run(self.store,'one');factory.assert_not_called()

if __name__=='__main__':unittest.main()

class UnsupportedCancellationTests(unittest.TestCase):
    setUp=ExternalCleanupTests.setUp
    tearDown=ExternalCleanupTests.tearDown
    def test_unsupported_cancel_is_explicit_and_not_retried(self):
        client=MagicMock();client.__enter__.return_value=client
        response=client.request.return_value.__enter__.return_value
        response.status_code=405;response.iter_content.return_value=[b'{"detail":"method not allowed"}']
        with patch('webapp.external_cleanup.session',return_value=client):external_cleanup.run(self.store,'one')
        self.assertEqual(self.store.query('SELECT state FROM execution_external_cleanup')[0]['state'],'unsupported')
        with patch('webapp.external_cleanup.session') as factory:external_cleanup.run(self.store,'one');factory.assert_not_called()

    def test_saved_wan_adapter_reports_unsupported_without_generic_cancel(self):
        self.store.execute("INSERT INTO workflow_provider_calls(request_key,workflow,step,revision,attempt,provider,operation,state,external_id,inputs,created,updated,implementation) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                           ('one','workflow','video',0,1,'http://127.0.0.1:9898','video.generate_from_image','waiting','job123','{}',0,0,json.dumps({'protocol':'wan-video-v1','provider':'http://127.0.0.1:9898','route_adapter':'generation','cleanup_category':'generation_protocol'})))
        client=MagicMock();client.__enter__.return_value=client
        with patch('webapp.external_cleanup.session',return_value=client):external_cleanup.run(self.store,'one')
        row=self.store.query('SELECT state,attempts FROM execution_external_cleanup')[0]
        self.assertEqual(row,{'state':'unsupported','attempts':1})
        client.request.assert_not_called();client.post.assert_not_called();client.delete.assert_not_called()
        with patch('webapp.external_cleanup.session') as factory:external_cleanup.run(self.store,'one');factory.assert_not_called()
