import json
import unittest

from webapp.runtime_injection import (
    CONTROL_TOKEN,GATEWAY_CA,GATEWAY_CONFIG,GATEWAY_TOKEN,HARNESS_CONFIG,RUNTIME_CONFIG,
    RUNTIME_PENDING,GatewayGrant,RuntimeBundle,RuntimeInjector,
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
            {'model':'model-1','reasoning_effort':'high','response_transport':'stream'},
            {'harness':'codex','runtime_profile':'agent-v1',
             'image_digest':'registry/agent@sha256:'+'a'*64,
             'skill_digest':'sha256:'+'b'*64,'tool_schema_digest':'sha256:'+'c'*64,
             'gateway_protocol':'gateway-v1'},
            {'model':'model-1','response_transport':'stream'},token='t'*48)

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

    def test_gateway_grant_is_separate_from_control_token_and_precedes_activation(self):
        base=self.bundle();gateway=GatewayGrant('https://172.30.0.1:18880','g'*48,b'public-ca',
            ('/v1/responses','/context'))
        bundle=RuntimeBundle.create(base.identity,base.harness,base.profile,base.binding,
            base.harness_config,token=base.control_token,gateway=gateway)
        files=FakeFiles();receipts=RuntimeInjector(files).inject(bundle,lambda _:None)
        self.assertTrue({GATEWAY_CA,GATEWAY_CONFIG,GATEWAY_TOKEN}<=set(receipts))
        runtime=json.loads(files.data[RUNTIME_CONFIG])
        self.assertEqual(runtime['gateway_config_file'],GATEWAY_CONFIG)
        self.assertNotIn(gateway.token,files.data[RUNTIME_CONFIG].decode())

    def test_task_output_and_scratch_exist_before_runtime_activation(self):
        base=self.bundle();output='/workspace/edit/workflows/run/task/attempt'
        bundle=RuntimeBundle.create(base.identity,base.harness,base.profile,base.binding,
            {**base.harness_config,'task_workspace':{'output':output,
                'inputs':{'source':['/workspace/sources/input.mp4']}}},token=base.control_token)
        files=FakeFiles();RuntimeInjector(files).inject(bundle,lambda _:None)
        self.assertIn(('mkdir',output,0o700),files.operations)
        self.assertIn(('mkdir',output+'/.scratch',0o700),files.operations)
        activation=files.operations.index(('move',RUNTIME_PENDING,RUNTIME_CONFIG))
        self.assertLess(files.operations.index(('mkdir',output+'/.scratch',0o700)),activation)

    def test_task_scratch_injection_rejects_output_escape(self):
        base=self.bundle()
        bundle=RuntimeBundle.create(base.identity,base.harness,base.profile,base.binding,
            {**base.harness_config,'task_workspace':{'output':'/tmp/work','inputs':{}}},token=base.control_token)
        with self.assertRaisesRegex(ValueError,'path policy'):
            RuntimeInjector(FakeFiles()).inject(bundle,lambda _:None)


if __name__=='__main__':unittest.main()
