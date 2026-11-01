import os
from pathlib import Path
import socket
import subprocess
import tempfile
import time
import requests

with tempfile.TemporaryDirectory(prefix='vu-browser-') as data:
    from tests.isolation import isolate_environment,isolate_network
    isolate_environment(Path(data))
    isolate_network()
    with socket.socket() as sock:sock.bind(('127.0.0.1',0));port=sock.getsockname()[1]
    base=f'http://127.0.0.1:{port}'
    env={**os.environ,'VIDEO_USE_DATA_DIR':data,'PYTHONDONTWRITEBYTECODE':'1'}
    with tempfile.TemporaryFile() as logs:
        process=subprocess.Popen(['.venv/bin/python','-m','uvicorn','webapp.app:app','--host','127.0.0.1','--port',str(port)],env=env,stdout=logs,stderr=logs)
        try:
            client=requests.Session();client.trust_env=False
            for _ in range(50):
                try:
                    if client.get(base+'/',timeout=1).ok:break
                except requests.RequestException:pass
                time.sleep(.1)
            result=subprocess.run(['node','tests/browser_workflow_smoke.cjs',base,data,str(Path.cwd())],capture_output=True,text=True,timeout=60)
            print(result.stdout or result.stderr)
            if result.returncode:raise SystemExit(result.returncode)
            Path('docs/test-evidence/browser-result.json').write_text(result.stdout)
        finally:
            process.terminate();process.wait(timeout=15)
