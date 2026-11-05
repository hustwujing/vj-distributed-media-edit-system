"""Narrow adapters around the synchronous OpenSandbox Python SDK."""
from __future__ import annotations

from datetime import timedelta
from urllib.parse import urlsplit,urlunsplit


class OpenSandboxFiles:
    def __init__(self,service,*,write_entry=None,move_entry=None,owner='video-use',group='video-use'):
        if write_entry is None:
            from opensandbox.models.filesystem import WriteEntry,MoveEntry
            write_entry,move_entry=WriteEntry,MoveEntry
        self.service=service;self.write_entry=write_entry;self.move_entry=move_entry
        self.owner=owner;self.group=group

    @staticmethod
    def _mode(value):
        if not isinstance(value,int) or value<0 or value>0o777:
            raise ValueError('Invalid sandbox file mode')
        return int(format(value,'o'))

    def mkdir(self,path,mode):
        self.service.create_directories([self.write_entry(path=path,mode=self._mode(mode),
            owner=self.owner,group=self.group)])

    def write(self,path,data,mode):
        self.service.write_files([self.write_entry(path=path,data=data,mode=self._mode(mode),
            owner=self.owner,group=self.group)])

    def read(self,path):
        return self.service.read_bytes(path)

    def delete(self,path,missing_ok=False):
        try:self.service.delete_files([path])
        except Exception as exc:
            if missing_ok and getattr(exc,'status_code',None)==404:return
            raise

    def move(self,source,target):
        self.service.move_files([self.move_entry(source=source,destination=target)])

    def list(self,path,depth=100):
        try:
            from opensandbox.models.filesystem import DirectoryListEntry
            entry=DirectoryListEntry(path=path,depth=depth)
        except ImportError:entry={'path':path,'depth':depth}
        return self.service.list_directory(entry)


class OpenSandboxSDKSession:
    def __init__(self,sandbox,config):self.sandbox=sandbox;self.config=config

    @property
    def files(self):return OpenSandboxFiles(self.sandbox.files)
    @property
    def commands(self):return self.sandbox.commands

    def websocket_endpoint(self,port,path='/control'):
        endpoint=self.sandbox.get_endpoint(port)
        raw=endpoint.endpoint
        if '://' not in raw:raw=self.config.protocol+'://'+raw
        parsed=urlsplit(raw)
        scheme={'http':'ws','https':'wss'}.get(parsed.scheme)
        if not scheme:raise ValueError('OpenSandbox endpoint must use HTTP or HTTPS')
        headers=endpoint.build_request_headers(self.config)
        target=urlunsplit((scheme,parsed.netloc,
            parsed.path.rstrip('/')+'/'+path.lstrip('/'),parsed.query,''))
        return target,headers

    def close(self):self.sandbox.close()


class OpenSandboxSDK:
    def __init__(self,base_url,api_key='',*,config_factory=None,sandbox_factory=None):
        self.base_url=base_url;self.api_key=api_key
        self.config_factory=config_factory;self.sandbox_factory=sandbox_factory

    def connect(self,sandbox_id):
        if self.config_factory is None:
            from opensandbox.config.connection_sync import ConnectionConfigSync
            self.config_factory=ConnectionConfigSync
        if self.sandbox_factory is None:
            from opensandbox.sync.sandbox import SandboxSync
            self.sandbox_factory=SandboxSync
        parsed=urlsplit(self.base_url)
        if parsed.scheme not in ('http','https') or not parsed.netloc:
            raise ValueError('Invalid OpenSandbox SDK base URL')
        config=self.config_factory(domain=self.base_url,protocol=parsed.scheme,
            api_key=self.api_key,use_server_proxy=True,follow_redirects=False,
            request_timeout=timedelta(seconds=30))
        sandbox=self.sandbox_factory.connect(sandbox_id,connection_config=config,
            connect_timeout=timedelta(seconds=30),skip_health_check=True)
        return OpenSandboxSDKSession(sandbox,config)
