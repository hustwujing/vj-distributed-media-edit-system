import json
import unittest

from webapp.runtime_injection import (
    CONTROL_TOKEN,HARNESS_CONFIG,RUNTIME_CONFIG,RUNTIME_PENDING,RuntimeBundle,RuntimeInjector,
)
from webapp.runtime_protocol import RuntimeIdentity


class FakeFiles:
    def __init__(self):self.data={};self.operations=[];self.corrupt=None
    def mkdir(self,path,mode):self.operations.append(('mkdir',path,mode))
    def write(self,path,data,mode):self.operations.append(('write',path,mode));self.data[path]=data
    def read(self,path):return self.data[path]+(b'x' if path==self.corrupt else b'')
    def delete(self,path,missing_ok=False):self.operations.append(('delete',path,missing_ok));self.data.pop(path,None)
    def move(self,source,target):
        self.operations.append(('move',source,target));self.data[target]=self.data.pop(source)


class RuntimeInjectionTests(unittest.TestCase):
    def bundle(self):
        return RuntimeBundle.create(RuntimeIdentity('attempt-1',1,'sandbox-1'),'codex',
            {'model':'model-1','reasoning_effort':'high'},
            {'harness':'codex','runtime_profile':'agent-v1',
             'image_digest':'registry/agent@sha256:'+'a'*64,
             'skill_digest':'sha256:'+'b'*64,'tool_schema_digest':'sha256:'+'c'*64,
             'gateway_protocol':'gateway-v1'},
            {'model':'model-1'},token='t'*48)

    def test_runtime_config_is_written_last_after_workspace_and_verification(self):
        files=FakeFiles();restored=[]
        def restore(adapter):
            restored.append(True);adapter.write('/workspace/input.txt',b'input',0o600)
        receipts=RuntimeInjector(files).inject(self.bundle(),restore)
        writes=[row[1] for row in files.operations if row[0]=='write']
        self.assertEqual(writes[-1],RUNTIME_PENDING)
        self.assertEqual(files.operations[-1],('move',RUNTIME_PENDING,RUNTIME_CONFIG))
        self.assertLess(writes.index('/workspace/input.txt'),writes.index(CONTROL_TOKEN))
        self.assertTrue(restored);self.assertEqual(set(receipts),
            {HARNESS_CONFIG,CONTROL_TOKEN,RUNTIME_CONFIG})
        config=json.loads(files.data[RUNTIME_CONFIG])
        self.assertEqual(config['control_token_file'],CONTROL_TOKEN)
        self.assertNotIn('t'*16,files.data[RUNTIME_CONFIG].decode())

    def test_verification_failure_never_writes_activation_marker(self):
        files=FakeFiles();files.corrupt=CONTROL_TOKEN
        with self.assertRaisesRegex(IOError,'verification'):
            RuntimeInjector(files).inject(self.bundle(),lambda _:None)
        self.assertNotIn(RUNTIME_CONFIG,files.data)

    def test_stale_activation_marker_is_removed_before_restore(self):
        files=FakeFiles();files.data[RUNTIME_CONFIG]=b'stale'
        RuntimeInjector(files).inject(self.bundle(),lambda _:None)
        self.assertEqual(files.operations[0],('delete',RUNTIME_CONFIG,True))


if __name__=='__main__':unittest.main()
