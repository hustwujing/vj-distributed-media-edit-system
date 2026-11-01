import base64
import io
import os
import unittest
from unittest.mock import patch
from webapp.object_encryption import encrypt,decrypt,configured_key,OVERHEAD


class ObjectEncryptionTests(unittest.TestCase):
    def setUp(self):self.key=os.urandom(32);self.data=b'private checkpoint contents'*100000
    def encoded(self):
        output=io.BytesIO();encrypt(io.BytesIO(self.data),output,self.key,'project/object');return output.getvalue()
    def test_stream_roundtrip_and_random_nonce(self):
        first=self.encoded();second=self.encoded();self.assertNotEqual(first,second)
        self.assertNotIn(b'private checkpoint',first);self.assertEqual(len(first),len(self.data)+OVERHEAD)
        out=io.BytesIO();decrypt(io.BytesIO(first),out,self.key,'project/object',len(self.data));self.assertEqual(out.read(),self.data)
    def test_wrong_key_identity_tamper_size_or_truncation_fail(self):
        raw=self.encoded();damaged=bytearray(raw);damaged[60]^=1
        for content,key,identity,limit in [(raw,os.urandom(32),'project/object',len(self.data)),(raw,self.key,'another/object',len(self.data)),
                (bytes(damaged),self.key,'project/object',len(self.data)),(raw[:-2],self.key,'project/object',len(self.data)),
                (raw,self.key,'project/object',2),(b'plaintext',self.key,'project/object',100)]:
            with self.assertRaises(ValueError):decrypt(io.BytesIO(content),io.BytesIO(),key,identity,limit)
    def test_invalid_configuration_and_cancel_during_encryption(self):
        for value in ('not-base64',base64.b64encode(b'short').decode()):
            with patch.dict(os.environ,{'VIDEO_USE_OBJECT_ENCRYPTION_KEY':value}),self.assertRaises(ValueError):configured_key()
        def stopped():raise RuntimeError('cancelled')
        with self.assertRaisesRegex(RuntimeError,'cancelled'):encrypt(io.BytesIO(self.data),io.BytesIO(),self.key,'object',stopped)
