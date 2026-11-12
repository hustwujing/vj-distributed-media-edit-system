import json
import os
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

    def test_opensandbox_attempt_is_deleted_without_provider_submission(self):
        attempt='a'*32
        self.store.execute('DELETE FROM execution_external_cleanup')
        self.store.execute("INSERT INTO execution_external_cleanup(id,task_id,provider,external_id,state,phase) VALUES (?,?,?,?,'pending','delete')",
            ('opensandbox:'+attempt,'task','opensandbox:'+attempt,'sandbox-1'))
        transport=MagicMock();client=MagicMock()
        with patch.dict(os.environ,{'VIDEO_USE_OPEN_SANDBOX_URL':'http://127.0.0.1:28090','VIDEO_USE_OPEN_SANDBOX_API_KEY':'key'}),\
             patch('webapp.sandbox_backends.RequestsTransport',return_value=transport),\
             patch('webapp.sandbox_backends.OpenSandboxClient',return_value=client),\
             patch('webapp.external_cleanup.session') as provider:
            external_cleanup.run(self.store,'opensandbox:'+attempt)
        client.delete_confirmed.assert_called_once_with('sandbox-1',expected_attempt=attempt)
        transport.close.assert_called_once();provider.assert_not_called()
        self.assertEqual(self.store.query('SELECT state,phase FROM execution_external_cleanup')[0],
            {'state':'complete','phase':'deleted'})

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

    def test_running_seedance_cancel_returns_to_poll_without_spending_failure_attempts(self):
        from webapp.generation_errors import CancellationDeferred
        self.store.execute("INSERT INTO workflow_provider_calls(request_key,workflow,step,revision,attempt,provider,operation,state,external_id,inputs,created,updated,implementation) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
            ('one','workflow','video',0,1,'http://127.0.0.1:9898','video.edit','running','job123','{}',0,0,
             json.dumps({'protocol':'ark-video-v1','provider':'http://127.0.0.1:9898','route_adapter':'generation','cleanup_category':'generation_protocol'})))
        client=MagicMock();client.__enter__.return_value=client
        with patch('webapp.external_cleanup.session',return_value=client),\
             patch('webapp.generation_service.cancel',side_effect=CancellationDeferred('running')):
            external_cleanup.run(self.store,'one')
        row=self.store.query('SELECT state,phase,attempts,error FROM execution_external_cleanup')[0]
        self.assertEqual((row['state'],row['phase'],row['attempts']),('pending','poll',0))
        self.assertIn('继续查询',row['error'])
