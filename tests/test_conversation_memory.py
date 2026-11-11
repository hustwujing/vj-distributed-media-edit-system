"""Token-triggered durable conversation compaction."""
import os
import unittest
from unittest.mock import patch
from tests import test_dynamic_graph as fixtures
from webapp import conversation_memory as memory,workflow_graph as graph


class ConversationMemoryTests(unittest.TestCase):
    def setUp(self):
        self.f=fixtures.DynamicGraphTests();self.f.setUp();self.addCleanup(self.f.tearDown)
        self.store=self.f.store;self.run=graph.get(self.store,self.f.run['id'])

    def add(self,identifier,content,created):
        self.store.execute('INSERT INTO session_messages(id,session_id,run_id,role,content,created_at) VALUES (?,?,?,?,?,?)',
            (identifier,self.run['session_id'],self.run['id'],'user',content,created))

    def test_token_threshold_schedules_background_compaction_and_advances_cursor_only_after_commit(self):
        baseline=len(self.store.query('SELECT id FROM session_messages WHERE session_id=?',(self.run['session_id'],)))
        now=self.store.now()
        for index in range(5):self.add('long-'+str(index),'海豚位置和交付约束必须保持一致。'*180,now+index)
        with patch.dict(os.environ,{'VIDEO_USE_CONVERSATION_COMPACT_TOKENS':'2000','VIDEO_USE_CONVERSATION_TAIL_TOKENS':'500'}):
            self.assertTrue(memory.schedule(self.store,self.run['session_id'],self.run['id']))
            self.assertFalse(self.store.query('SELECT session_id FROM conversation_summaries WHERE session_id=?',(self.run['session_id'],)))
            self.assertTrue(memory.process_pending(self.store,self.run['session_id']))
        summary=self.store.query('SELECT * FROM conversation_summaries WHERE session_id=?',(self.run['session_id'],))[0]
        self.assertEqual(summary['status'],'committed')
        self.assertGreater(len(self.store.query('SELECT id FROM session_messages WHERE session_id=?',(self.run['session_id'],))),baseline)
        view=memory.snapshot(self.store,graph.get(self.store,self.run['id']))
        self.assertTrue(view['summary']['message']);self.assertEqual(view['summary']['history_message_count'],5+baseline)
        self.assertTrue(all(row['created_at']>summary['through_created_at'] or
            row['created_at']==summary['through_created_at'] and row['id']>summary['through_message_id']
            for row in view['messages_after_cursor']))

    def test_digest_mismatch_never_moves_the_committed_cursor_or_deletes_raw_messages(self):
        now=self.store.now()
        for index in range(4):self.add('guard-'+str(index),'持续会话事实。'*500,now+index)
        before=len(self.store.query('SELECT id FROM session_messages WHERE session_id=?',(self.run['session_id'],)))
        with patch.dict(os.environ,{'VIDEO_USE_CONVERSATION_COMPACT_TOKENS':'2000','VIDEO_USE_CONVERSATION_TAIL_TOKENS':'500'}):
            self.assertTrue(memory.schedule(self.store,self.run['session_id'],self.run['id']))
            self.store.execute("UPDATE conversation_summary_jobs SET source_digest='stale' WHERE session_id=?",(self.run['session_id'],))
            self.assertFalse(memory.process_pending(self.store,self.run['session_id']))
        self.assertFalse(self.store.query('SELECT session_id FROM conversation_summaries WHERE session_id=?',(self.run['session_id'],)))
        self.assertEqual(len(self.store.query('SELECT id FROM session_messages WHERE session_id=?',(self.run['session_id'],))),before)
        self.assertEqual(self.store.query('SELECT status FROM conversation_summary_jobs WHERE session_id=?',(self.run['session_id'],))[0]['status'],'failed')


if __name__=='__main__':unittest.main()
