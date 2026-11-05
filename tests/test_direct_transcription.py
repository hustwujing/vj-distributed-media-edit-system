"""Direct transcription contract and durable receipt tests; provider stub only in unit tests."""
import json
import os
from pathlib import Path
import threading
import unittest
from unittest.mock import patch
import wave

from tests import test_workflows as fixtures
from webapp.agent_gateway import Gateway
from webapp.execution_intent import declare, completion_problem
from webapp.task_leases import Coordinator


def payload(result):return json.loads(b''.join(result.chunks))


class DirectTranscriptionTests(unittest.TestCase):
    def setUp(self):
        self.f=fixtures.WorkflowTests();self.f.setUp();s=self.f.store
        s.execute("UPDATE agent_turns SET status='succeeded' WHERE id=?",(self.f.turn,))
        s.execute("UPDATE agent_turns SET status='running' WHERE id=?",(self.f.plan,))
        declare(s,self.f.plan,{'route':'single','operations':['audio.transcribe'],'goal':'transcribe English speech','estimated_seconds':90})
        s.execute("UPDATE agent_turns SET status='succeeded' WHERE id=?",(self.f.plan,))
        s.execute("UPDATE agent_turns SET status='queued' WHERE id=?",(self.f.turn,))
        self.lease=Coordinator(s,'transcription-tests','tests').claim(self.f.turn,'agent')
        s.execute("UPDATE agent_turns SET status='running' WHERE id=?",(self.f.turn,))
        self.g=Gateway.__new__(Gateway);g=self.g
        g.lease=self.lease;g.mode='execute';g.project_dir=s.project_dir(self.f.project)
        (g.project_dir/'edit/transcripts').mkdir(parents=True,exist_ok=True)
        g.assets={'speech':{'id':'speech','name':'speech.wav','filename':'speech.wav','metadata':{'kind':'audio','duration':1,'audio_tracks':[{}]}}}
        g.transcribe_lock=threading.Lock();g.notify=lambda *_:None;g.uid=os.getuid();g.gid=os.getgid()
        def extract(filename,track,destination):
            with wave.open(str(destination),'wb') as out:
                out.setnchannels(1);out.setsampwidth(2);out.setframerate(16000);out.writeframes(b'\x00\x10'*16000)
        g.extract_audio=extract
        import sys
        sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'helpers'))
    def tearDown(self):
        self.lease.release();self.f.tearDown()
    def calls(self):return self.f.store.query('SELECT state,operation,parameters,network_seconds FROM workflow_provider_calls WHERE workflow=?',(self.f.turn,))
    def test_receipt_precedes_request_and_success_is_durable(self):
        def provider(*args):
            self.assertEqual(self.calls()[0]['state'],'submitting')
            return {'text':'Real contract test','words':[],'usage':{'seconds':1}}
        with self.lease.bind(),patch.dict(os.environ,{'ELEVENLABS_API_KEY':'unit-only'}),patch('transcribe.call_scribe',side_effect=provider):
            response=self.g.transcribe_result({'asset':'speech','language':'en','speakers':1})
        self.assertEqual(response.status,200)
        self.assertEqual(self.calls()[0]['state'],'succeeded')
        self.assertEqual(self.calls()[0]['operation'],'audio.transcribe')
        self.assertEqual(json.loads(self.calls()[0]['parameters'])['language'],'en')
        self.assertIsNone(completion_problem(self.f.store,self.f.turn,[{'name':'speech.txt'}]))
    def test_ambiguous_timeout_preserves_submission_and_blocks_false_completion(self):
        with self.lease.bind(),patch.dict(os.environ,{'ELEVENLABS_API_KEY':'unit-only'}),patch('transcribe.call_scribe',side_effect=TimeoutError('unit timeout')):
            with self.assertRaises(TimeoutError):self.g.transcribe_result({'asset':'speech'})
        self.assertEqual(self.calls()[0]['state'],'submitting')
        self.assertEqual(completion_problem(self.f.store,self.f.turn,[{'name':'error.json'}])[0],'paused')
    def test_wrong_intent_is_actionable_and_never_calls_provider(self):
        # Immutable intent mismatch is rejected before decode/network; it is not malformed JSON.
        raw={'route':'single','operations':['script'],'goal':'local','estimated_seconds':30}
        self.f.store.execute('UPDATE execution_intents SET intent=? WHERE run_id=?',(json.dumps(raw),self.f.plan))
        with self.lease.bind(),patch('transcribe.call_scribe') as provider:
            response=self.g.transcribe_result({'asset':'speech'})
        self.assertEqual(response.status,422)
        self.assertIn('audio.transcribe',payload(response)['error']);provider.assert_not_called()
        self.assertFalse(self.calls())
    def test_manual_batch_keeps_separate_receipts_for_each_asset(self):
        self.lease.release();s=self.f.store
        s.execute("UPDATE agent_turns SET status='succeeded' WHERE id=?",(self.f.turn,))
        job=s.add_job(self.f.project,'transcribe',{'kind':'transcribe','assets':['speech','second'],'audio_track':0,'language':'en','speakers':1,'workers':1})
        self.lease=Coordinator(s,'manual-transcription-tests','tests').claim(job['id'],'manual')
        self.assertIsNotNone(self.lease)
        s.execute("UPDATE jobs SET status='running' WHERE id=?",(job['id'],))
        self.g.lease=self.lease
        self.g.assets['second']={**self.g.assets['speech'],'id':'second','name':'second.wav','filename':'second.wav'}
        with self.lease.bind(),patch.dict(os.environ,{'ELEVENLABS_API_KEY':'unit-only'}),patch('transcribe.call_scribe',return_value={'text':'test','words':[]}):
            for asset in ('speech','second'):
                response=self.g.transcribe_result({'asset':asset});self.assertEqual(response.status,200)
        rows=s.query('SELECT request_key,state,authorization FROM workflow_provider_calls WHERE workflow=?',(job['id'],))
        self.assertEqual(len(rows),2);self.assertEqual(len({r['request_key'] for r in rows}),2)
        self.assertTrue(all(r['state']=='succeeded' and json.loads(r['authorization'])['origin']=='manual' for r in rows))


class TranscriptionClientTests(unittest.TestCase):
    @unittest.skipUnless(os.geteuid()==0,'Requires real uid transition')
    def test_new_transcript_created_by_unprivileged_gateway(self):
        import pwd,subprocess,sys,tempfile
        account=pwd.getpwnam('video-use')
        with tempfile.TemporaryDirectory(prefix='transcript-new-') as directory:
            root=Path(directory);root.chmod(0o755)
            folder=root/'edit/transcripts';folder.mkdir(parents=True)
            folder.parent.chmod(0o755)
            os.chown(folder,account.pw_uid,account.pw_gid)
            program='''import json,os,sys,threading,wave
from pathlib import Path
from unittest.mock import Mock,patch
from webapp.agent_gateway import Gateway
assert os.geteuid()!=0
root=Path(sys.argv[1]);sys.path.insert(0,str(Path.cwd()/'helpers'))
g=Gateway.__new__(Gateway);g.project_dir=root;g.mode='execute';g.transcribe_lock=threading.Lock()
g.assets={'speech':{'id':'speech','name':'speech.wav','filename':'speech.wav','metadata':{'kind':'audio','duration':1,'audio_tracks':[{}]}}}
g.notify=Mock();g.broker=Mock()
def extract(filename,track,path):
    with wave.open(str(path),'wb') as out:
        out.setnchannels(1);out.setsampwidth(2);out.setframerate(16000);out.writeframes(b'\\x00\\x10'*16000)
g.extract_audio=extract
with patch.dict(os.environ,{'ELEVENLABS_API_KEY':'test-only'}),patch('transcribe.call_scribe',return_value={'text':'new','words':[]}) as provider:
    result=g.transcribe_result({'asset':'speech'})
    assert result.status==200,result.status
    provider.assert_called_once()
g.broker.request.assert_called_once_with('refresh_transcript',{'asset':'speech','track':0})
path=root/'edit/transcripts/speech.json'
assert json.loads(path.read_text())['text']=='new'
assert path.stat().st_uid==os.geteuid()
assert path.stat().st_mode&0o777==0o640
'''
            result=subprocess.run([sys.executable,'-c',program,str(root)],user=account.pw_uid,group=account.pw_gid,
                                  extra_groups=[],capture_output=True,text=True,timeout=20)
            self.assertEqual(result.returncode,0,result.stderr)

    def test_client_surfaces_safe_gateway_reason(self):
        import io,runpy,sys
        from urllib.error import HTTPError
        reason='请求能力 audio.transcribe 与已确认意图不符'
        error=HTTPError('http://127.0.0.1:18766/transcribe',422,'Unprocessable',{},io.BytesIO(json.dumps({'error':reason}).encode()))
        with patch.object(sys,'argv',['transcribe.py','owned-asset']),patch('urllib.request.urlopen',side_effect=error):
            with self.assertRaises(SystemExit) as result:runpy.run_module('webapp.agent_transcribe',run_name='__main__')
        self.assertIn(reason,str(result.exception));self.assertIn('422',str(result.exception))
