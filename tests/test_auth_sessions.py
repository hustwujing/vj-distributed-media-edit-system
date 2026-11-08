from concurrent.futures import ThreadPoolExecutor
import hashlib
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch

from webapp.auth import Sessions
from webapp.store import Store


class SessionIdentityTests(unittest.TestCase):
    def setUp(self):
        self.enterContext(patch.dict('os.environ',{'VIDEO_USE_SESSION_SECRET':''}))
        folder=tempfile.TemporaryDirectory();self.addCleanup(folder.cleanup)
        self.store=Store(Path(folder.name))
        self.token='a'*64
        self.owner=hashlib.sha256(self.token.encode()).hexdigest()
        self.project=self.store.add_project(self.owner,'owned')['id']
        self.sessions=Sessions(self.store)

    def test_repeated_raw_and_signed_requests_keep_owner_across_restart(self):
        first=self.sessions.resolve(self.token)
        second=self.sessions.resolve(self.token)
        signed=self.sessions.resolve(first[1])
        restarted=Sessions(Store(self.store.root)).resolve(self.token)
        self.assertEqual([first[0],second[0],signed[0],restarted[0]],[self.owner]*4)
        self.assertFalse(signed[2])
        self.assertEqual(self.store.query('SELECT id FROM projects WHERE owner=?',(second[0],))[0]['id'],self.project)

    def test_simultaneous_initial_requests_resolve_same_owner(self):
        barrier=threading.Barrier(8)
        def resolve(_):
            barrier.wait(timeout=5)
            return self.sessions.resolve(self.token)
        with ThreadPoolExecutor(max_workers=8) as pool:responses=list(pool.map(resolve,range(8)))
        self.assertEqual({row[0] for row in responses},{self.owner})
        self.assertEqual({row[1] for row in responses},{self.sessions.sign(self.token)})

    def test_unsigned_access_stays_disabled_with_shared_signing_secret(self):
        with patch.dict('os.environ',{'VIDEO_USE_SESSION_SECRET':'b'*64}):
            configured=Sessions(self.store)
        self.assertNotEqual(configured.resolve(self.token)[0],self.owner)
        self.assertEqual(configured.resolve(configured.sign(self.token))[0],self.owner)

    def test_unknown_raw_or_invalid_signed_cookie_cannot_claim_existing_owner(self):
        for cookie in ('c'*64,self.sessions.sign(self.token)[:-1]+'x'):
            with self.subTest(cookie_type='signed' if '.' in cookie else 'raw'):
                self.assertNotEqual(self.sessions.resolve(cookie)[0],self.owner)
