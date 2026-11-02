"""Explicit isolated browser acceptance; requires Nginx and Playwright Chromium."""
import sys,os,json,threading,subprocess,time
from pathlib import Path
sys.path.insert(0,str(Path.cwd()))
from tests.isolation import isolate_environment,isolate_network
out=Path(os.environ.get('VU_API_BROWSER_OUTPUT','docs/test-evidence/api-browser')).resolve();out.mkdir(exist_ok=True,parents=True)
isolate_environment(out/'environment');isolate_network();os.environ['VIDEO_USE_TELEMETRY_ENABLED']='1'
from tests.test_api_pressure import APIPressureTests
from webapp.observation_collector import Archive,handler
from webapp.api_capacity import views
from http.server import ThreadingHTTPServer
case=APIPressureTests('test_two_apis_preserve_control_and_bound_all_slow_dependencies')
try:
 case.setUp();turn=case.task();archive=Archive(out/'archive')
 # Only availability is a fixture; actual message submission still uses HTTP/PG.
 # No executor runs, so this cannot call a model or execute the queued prompt.
 case.store.execute('INSERT INTO execution_workers(id,node,release,model,heartbeat,draining) VALUES (?,?,?,?,?,0)',('browser-fixture-worker','browser-fixture-node','browser-test','fixture-model',case.store.now()+3600))
 # Actual API processes emit their capacity snapshots; do not fabricate UI data.
 deadline=time.monotonic()+8
 while time.monotonic()<deadline:
  for file in sorted(case.root.glob('api-*.log')):
   for line in file.read_text().splitlines():archive.ingest(line)
  with archive.connect() as db:rows=[json.loads(row[0]) for row in db.execute('SELECT body FROM api_capacity')]
  value=views(rows)
  if len(value['complete_instances'])==2:break
  time.sleep(.1)
 assert len(value['complete_instances'])==2,value
 archive.snapshot={'views':{'api_capacity':value}}
 observer=ThreadingHTTPServer(('127.0.0.1',0),handler(archive,'isolated-observer-token'));thread=threading.Thread(target=observer.serve_forever,daemon=True);thread.start()
 try:
  config={'base':case.proxy,'bases':case.bases,'project':case.project,'video':case.video,'turn':turn,'signal':str(case.signals/'media'),'output':str(out),'observer':f'http://127.0.0.1:{observer.server_port}','cookies':[{'name':c.name,'value':c.value,'domain':'127.0.0.1','path':'/'} for c in case.session.cookies]}
  path=out/'private-config.json';path.write_text(json.dumps(config));path.chmod(0o600)
  result=subprocess.run(['node',str(Path('tests/browser_api_isolation.cjs').resolve()),str(path)],capture_output=True,text=True,timeout=75)
  print(result.stdout);print(result.stderr)
  if result.returncode:raise SystemExit(result.returncode)
 finally:observer.shutdown();observer.server_close();thread.join()
finally:case.doCleanups()
