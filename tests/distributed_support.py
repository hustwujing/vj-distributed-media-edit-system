"""Disposable real PostgreSQL and HTTP S3 fixtures; never use deployment data."""
import os
from pathlib import Path
import pwd
import shutil
import socket
import subprocess
import tempfile
import time


def free_port():
    with socket.socket() as sock:
        sock.bind(('127.0.0.1', 0))
        return sock.getsockname()[1]


class PostgresServer:
    def __init__(self):
        candidates = list(Path('/usr/lib/postgresql').glob('*/bin/initdb'))
        executable = shutil.which('initdb') or (str(sorted(candidates)[-1]) if candidates else '')
        if not executable:
            raise RuntimeError('Real PostgreSQL initdb is required for distributed integration tests')
        self.bin = Path(executable).parent
        self.temp = tempfile.TemporaryDirectory(prefix='vu-distributed-pg-')
        self.root = Path(self.temp.name)
        self.port = free_port()
        self.prefix = []
        if os.geteuid() == 0:
            account = pwd.getpwnam('postgres')
            os.chown(self.root, account.pw_uid, account.pw_gid)
            self.prefix = ['runuser', '-u', 'postgres', '--']
        self.command('initdb', '-D', str(self.root/'data'), '-A', 'trust', '-U', 'postgres', '--no-locale', '--encoding=UTF8')
        self.start()
        self.dsn = f'postgresql://postgres@127.0.0.1:{self.port}/postgres'

    def command(self, name, *args):
        return subprocess.run([*self.prefix, str(self.bin/name), *args], check=True,
                              capture_output=True, timeout=30)

    def start(self):
        self.command('pg_ctl','-D',str(self.root/'data'),'-l',str(self.root/'server.log'),
                     '-o',f'-h 127.0.0.1 -p {self.port} -k {self.root} -c fsync=on','-w','start')

    def close(self):
        if (self.root/'data/postmaster.pid').exists():
            self.command('pg_ctl', '-D', str(self.root/'data'), '-m', 'immediate', '-w', 'stop')
        self.temp.cleanup()


class S3Server:
    def __init__(self):
        self.port = free_port()
        self.endpoint = f'http://127.0.0.1:{self.port}'
        executable = str(Path(__import__('sys').executable).parent/'moto_server')
        self.log = tempfile.TemporaryFile()
        self.process = subprocess.Popen([executable, '-H', '127.0.0.1', '-p', str(self.port)],
                                        stdout=self.log, stderr=self.log)
        import requests
        for _ in range(100):
            try:
                if requests.get(self.endpoint, timeout=.2).status_code == 200:
                    break
            except requests.RequestException:
                pass
            time.sleep(.05)
        else:
            self.close()
            raise RuntimeError('S3 protocol test service failed to start')

    def close(self):
        self.process.terminate()
        try:self.process.wait(timeout=5)
        except subprocess.TimeoutExpired:self.process.kill();self.process.wait(timeout=5)
        self.log.close()
