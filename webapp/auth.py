"""Signed browser identities, matching the pattern used by adjacent lab tools."""
import hashlib
import hmac
import os
import re
import secrets
from pathlib import Path


class Sessions:
    def __init__(self, store):
        self.store = store
        self.legacy_enabled=False
        configured=os.environ.get('VIDEO_USE_SESSION_SECRET','')
        if configured:
            if not re.fullmatch(r'[a-fA-F0-9]{64}',configured):raise ValueError('VIDEO_USE_SESSION_SECRET must contain 32 bytes encoded as 64 hex characters')
            self.secret=bytes.fromhex(configured)
            return
        if store.dsn:raise ValueError('API replicas require a shared VIDEO_USE_SESSION_SECRET')
        root = store.root
        self.legacy_enabled=True
        store.execute('CREATE TABLE IF NOT EXISTS legacy_sessions (owner TEXT PRIMARY KEY)')
        with store.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            path = root / 'session-secret'
            try:
                fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            except FileExistsError:
                pass
            else:
                with os.fdopen(fd, 'wb') as stream:
                    stream.write(secrets.token_bytes(32))
                    stream.flush();os.fsync(stream.fileno())
                store.execute('INSERT OR IGNORE INTO legacy_sessions SELECT DISTINCT owner FROM projects')
            self.secret = path.read_bytes()
            if len(self.secret)!=32:raise RuntimeError('Invalid session signing key; restore the saved key before starting')

    def sign(self, identity):
        signature = hmac.new(self.secret, identity.encode(), hashlib.sha256).hexdigest()
        return identity + '.' + signature

    def resolve(self, token):
        token = token or ''
        if '.' in token:
            identity, signature = token.rsplit('.', 1)
            if re.fullmatch(r'[a-f0-9]{64}', identity) and hmac.compare_digest(self.sign(identity), token):
                return hashlib.sha256(identity.encode()).hexdigest(), token, False
        # Preserve existing projects when upgrading the previous random bearer cookie.
        # Possessing that high-entropy token was already the old access credential.
        legacy = False
        if not self.legacy_enabled:
            identity=secrets.token_hex(32)
            return hashlib.sha256(identity.encode()).hexdigest(),self.sign(identity),True
        if re.fullmatch(r'[a-f0-9]{64}', token):
            owner = hashlib.sha256(token.encode()).hexdigest()
            legacy = bool(self.store.execute('DELETE FROM legacy_sessions WHERE owner=?', (owner,)))
        identity = token if legacy else secrets.token_hex(32)
        return hashlib.sha256(identity.encode()).hexdigest(), self.sign(identity), True
