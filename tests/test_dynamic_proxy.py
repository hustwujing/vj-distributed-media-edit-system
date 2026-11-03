"""A live temporary nginx proves overload does not eject healthy API replicas."""
from http.server import BaseHTTPRequestHandler,ThreadingHTTPServer
from pathlib import Path
import shutil,socket,subprocess,tempfile,threading,time,unittest
from urllib.request import build_opener,ProxyHandler
from urllib.error import HTTPError,URLError

@unittest.skipUnless(shutil.which('nginx'),'nginx required for proxy integration')
class DynamicProxyTests(unittest.TestCase):
 def test_capacity_503_keeps_peers_available_for_next_requests(self):
  from deploy.dynamic_cluster import nginx_config,PORTS,PROXY_PORT
  urlopen=build_opener(ProxyHandler({})).open
  class Backend(BaseHTTPRequestHandler):
   def do_GET(self):
    status=503 if self.path=='/busy' else 200;body=b'bounded overload' if status==503 else b'healthy'
    self.send_response(status);self.send_header('Content-Length',str(len(body)));self.end_headers();self.wfile.write(body)
   def log_message(self,*args):pass
  servers=[ThreadingHTTPServer(('127.0.0.1',0),Backend) for _ in range(2)]
  for server in servers:threading.Thread(target=server.serve_forever,daemon=True).start()
  try:
   with tempfile.TemporaryDirectory(prefix='dynamic-proxy-test-') as folder:
    root=Path(folder)
    with socket.socket() as sock:sock.bind(('127.0.0.1',0));port=sock.getsockname()[1]
    configuration=nginx_config()
    for old,new in [(PORTS['a'],servers[0].server_port),(PORTS['b'],servers[1].server_port),(PROXY_PORT,port)]:configuration=configuration.replace(':'+str(old),':'+str(new))
    config=root/'nginx.conf';config.write_text(f'pid {root}/nginx.pid;\nerror_log {root}/error.log;\nevents {{}}\nhttp {{ access_log off;\n'+configuration+'\n}\n')
    process=subprocess.Popen(['nginx','-c',str(config),'-p',folder,'-g','daemon off; master_process off;'],stdout=subprocess.DEVNULL,stderr=subprocess.PIPE)
    try:
     for _ in range(100):
      try:
       with urlopen(f'http://127.0.0.1:{port}/ok',timeout=.2) as r:
        if r.status==200:break
      except (URLError,OSError):time.sleep(.02)
     else:self.fail('Temporary nginx did not start')
     for _ in range(8):
      with self.assertRaises(HTTPError) as rejected:urlopen(f'http://127.0.0.1:{port}/busy',timeout=2)
      self.assertEqual(rejected.exception.code,503,'503 must not turn into no-live-upstreams 502');rejected.exception.close()
     with urlopen(f'http://127.0.0.1:{port}/ok',timeout=2) as response:self.assertEqual(response.read(),b'healthy')
    finally:
     process.terminate();process.communicate(timeout=5)
  finally:
   for server in servers:server.shutdown();server.server_close()
