import unittest
from types import SimpleNamespace

from webapp.opensandbox_sdk import OpenSandboxFiles,OpenSandboxSDK,OpenSandboxSDKSession


class Entry:
    def __init__(self,**values):self.__dict__.update(values)


class FileService:
    def __init__(self):self.calls=[];self.data={}
    def create_directories(self,entries):self.calls.append(('mkdir',entries[0].__dict__))
    def write_files(self,entries):
        entry=entries[0];self.calls.append(('write',entry.__dict__));self.data[entry.path]=entry.data
    def read_bytes(self,path):return self.data[path]
    def delete_files(self,paths):self.calls.append(('delete',paths));self.data.pop(paths[0],None)
    def move_files(self,entries):
        entry=entries[0];self.calls.append(('move',entry.__dict__));self.data[entry.destination]=self.data.pop(entry.source)


class Endpoint:
    endpoint='sandbox.example/v1/sandboxes/sbx/proxy/18770'
    def build_request_headers(self,config):return {'OPEN-SANDBOX-API-KEY':config.api_key,'X-Route':'one'}


class Sandbox:
    def __init__(self):self.files=FileService();self.closed=False
    def get_endpoint(self,port):self.port=port;return Endpoint()
    def close(self):self.closed=True


class OpenSandboxSDKTests(unittest.TestCase):
    def test_files_adapter_preserves_modes_and_atomic_move(self):
        service=FileService();files=OpenSandboxFiles(service,write_entry=Entry,move_entry=Entry)
        files.mkdir('/workspace',0o700);files.write('/run/pending',b'value',0o600)
        self.assertEqual(files.read('/run/pending'),b'value')
        files.move('/run/pending','/run/ready');files.delete('/run/ready')
        self.assertEqual([call[0] for call in service.calls],['mkdir','write','move','delete'])
        self.assertEqual(service.calls[0][1]['mode'],700)
        self.assertEqual(service.calls[1][1]['mode'],600)
        self.assertEqual((service.calls[1][1]['owner'],service.calls[1][1]['group']),
            ('video-use','video-use'))
        with self.assertRaisesRegex(ValueError,'mode'):files.write('/bad',b'',0o1000)

    def test_endpoint_uses_server_proxy_path_and_required_headers(self):
        sandbox=Sandbox();config=SimpleNamespace(protocol='https',api_key='key')
        session=OpenSandboxSDKSession(sandbox,config)
        url,headers=session.websocket_endpoint(18770)
        self.assertEqual(url,'wss://sandbox.example/v1/sandboxes/sbx/proxy/18770/control')
        self.assertEqual(headers,{'OPEN-SANDBOX-API-KEY':'key','X-Route':'one'})
        self.assertEqual(sandbox.port,18770)

    def test_connector_forces_server_proxy_and_skips_daemon_health(self):
        calls={}
        def config_factory(**kwargs):calls['config']=kwargs;return SimpleNamespace(**kwargs)
        class Factory:
            @classmethod
            def connect(cls,sandbox_id,**kwargs):calls['connect']=(sandbox_id,kwargs);return Sandbox()
        session=OpenSandboxSDK('https://sandbox.example/v1','key',
            config_factory=config_factory,sandbox_factory=Factory).connect('sbx-1')
        self.assertTrue(calls['config']['use_server_proxy'])
        self.assertFalse(calls['config']['follow_redirects'])
        self.assertTrue(calls['connect'][1]['skip_health_check'])
        session.close();self.assertTrue(session.sandbox.closed)


if __name__=='__main__':unittest.main()
