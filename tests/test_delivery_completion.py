"""Host finalization regression using a model protocol double, not a live LLM."""
import io
import json
import os
import queue
import unittest
from unittest.mock import patch

from tests import test_workflows as fixtures
from webapp import agent_store
from webapp.agent_host import AgentHost
from webapp.execution_intent import declare


@unittest.skipUnless(os.geteuid()==0,'Host setup requires launcher privileges')
class HostDeliveryTests(unittest.TestCase):
    def test_completed_model_with_invalid_manifest_is_failed_with_preserved_candidates(self):
        fixture=fixtures.WorkflowTests();fixture.setUp()
        try:
            store=fixture.store
            store.execute("UPDATE agent_turns SET status='succeeded' WHERE id=?",(fixture.turn,))
            store.execute("UPDATE agent_turns SET status='running' WHERE id=?",(fixture.plan,))
            declare(store,fixture.plan,{'route':'single','operations':['script'],'goal':'Write a result document','estimated_seconds':30})
            store.execute("UPDATE agent_turns SET status='succeeded' WHERE id=?",(fixture.plan,))
            store.execute("UPDATE agent_turns SET status='running' WHERE id=?",(fixture.turn,))
            host=AgentHost(store.root);edit=store.project_dir(fixture.project)/'agent/workspace/edit'
            class Output:
                def __init__(self):self.queue=queue.Queue()
                def __iter__(self):
                    while (line:=self.queue.get()) is not None:yield line
                def close(self):pass
            class Process:
                def __init__(self):self.stdout=Output();self.stderr=io.StringIO('');self.stdin=self;self.returncode=None
                def write(self,line):
                    message=json.loads(line)
                    if 'id' not in message:return
                    result={'thread':{'id':'delivery-thread'}} if message['method'].startswith('thread/') else {}
                    self.stdout.queue.put(json.dumps({'id':message['id'],'result':result}))
                    if message['method']=='turn/start':
                        (edit/'result.txt').write_text('Actual useful generated result')
                        (edit/'deliverables.json').write_text(json.dumps({'primary':'edit/result.txt','files':[{'path':'edit/result.txt'}]}))
                        self.stdout.queue.put(json.dumps({'method':'item/completed','params':{'item':{'id':'answer','type':'agentMessage','text':'已完成并交付 result.txt'}}}))
                        self.stdout.queue.put(json.dumps({'method':'turn/completed','params':{'turn':{'status':'completed'}}}))
                        self.returncode=0;self.stdout.queue.put(None)
                def flush(self):pass
                def close(self):pass
                def poll(self):return self.returncode
                def wait(self,timeout=None):return self.returncode
            with patch('webapp.agent_host.Gateway'),patch('webapp.agent_host.subprocess.Popen',side_effect=lambda *a,**kw:Process()):
                host.run_turn(fixture.turn)
            row=agent_store.turn(store,fixture.turn)
            self.assertEqual(row['status'],'failed');self.assertIn('交付声明未通过检查',row['error'])
            self.assertTrue((edit/'result.txt').exists());self.assertTrue((edit/'deliverables.json').exists())
            candidate=next(a for a in row['artifacts'] if a['name']=='result.txt')
            self.assertEqual(candidate['quality_state'],'pending')
            self.assertEqual((store.project_dir(fixture.project)/'edit/agent_exports'/fixture.turn/candidate['file']).read_text(),'Actual useful generated result')
            public=agent_store.public(row)
            self.assertTrue(all(a['is_process'] and not a['is_primary'] for a in public['artifacts']))
        finally:fixture.tearDown()
