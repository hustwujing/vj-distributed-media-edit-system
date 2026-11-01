import json
from pathlib import Path
from types import SimpleNamespace
import tempfile
import unittest
from tests.campaign_fault import stop_once,read


class CampaignFaultTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup);self.folder=Path(self.temp.name)
        self.task='a'*32;self.candidate={'unit':'video-use-executor@a.service','id':'attempt','ready':True};self.calls=[];self.signal=9;self.after=False;self.stopped=False
    def fake_run(self,args,**kwargs):
        self.calls.append(args)
        if args[1]=='kill':self.after=True;return SimpleNamespace(returncode=1,stdout='',stderr='control process no longer exists')
        if args[1]=='stop':self.stopped=True;return SimpleNamespace(returncode=0,stdout='',stderr='')
        state='inactive' if self.stopped else 'failed' if self.after else 'active';pid='0' if self.after or self.stopped else '123'
        return SimpleNamespace(returncode=0,stdout=f'MainPID={pid}\nActiveState={state}\nSubState=dead\nExecMainCode={2 if self.after else 0}\nExecMainStatus={self.signal if self.after else 0}\n',stderr='')
    def test_nonzero_exit_after_signal_is_verified_and_never_repeated(self):
        result=stop_once(self.folder,self.task,self.candidate,self.fake_run)
        self.assertTrue(result['stopped']);self.assertEqual(result['kill_returncode'],1)
        count=len(self.calls);self.assertEqual(stop_once(self.folder,self.task,self.candidate,self.fake_run),result);self.assertEqual(len(self.calls),count)
    def test_uncertain_signal_is_not_reported_as_success_or_replayed(self):
        self.signal=15;result=stop_once(self.folder,self.task,self.candidate,self.fake_run)
        self.assertFalse(result['stopped']);self.assertEqual(result['fault_state'],'uncertain');self.assertTrue(self.stopped)
        count=len(self.calls);stop_once(self.folder,self.task,self.candidate,self.fake_run);self.assertEqual(len(self.calls),count)
    def test_interrupted_operator_keeps_intent_and_stops_auto_restart(self):
        def fail(args,**kwargs):
            if args[1]=='kill':raise KeyboardInterrupt()
            return self.fake_run(args,**kwargs)
        with self.assertRaises(KeyboardInterrupt):stop_once(self.folder,self.task,self.candidate,fail)
        self.assertTrue(self.stopped);self.assertEqual(read(self.folder,self.task)['fault_state'],'prepared')
        count=len(self.calls);result=stop_once(self.folder,self.task,self.candidate,self.fake_run);self.assertFalse(result['stopped']);self.assertEqual(len(self.calls),count)
    def test_other_service_cannot_be_a_fault_target(self):
        with self.assertRaises(ValueError):stop_once(self.folder,self.task,{'unit':'postgresql.service'},self.fake_run)
        self.assertEqual(self.calls,[])
