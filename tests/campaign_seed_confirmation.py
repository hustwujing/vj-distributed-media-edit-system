"""Real model and browser confirmation of an initially seeded shared project."""
import os,json,shutil,subprocess,tempfile,threading,time
from pathlib import Path
from tests.suite_lock import suite_lock
from tests.distributed_support import PostgresServer,S3Server,free_port
from tests.isolation import isolate_environment
from tests.test_demo_library import source_library
from deploy.dynamic_cluster import environment


def main():
 out=Path('.cluster/campaigns/20261101-seed-confirmation').resolve();out.mkdir(parents=True,exist_ok=True,mode=0o700)
 configured=environment();model={k:v for k,v in configured.items() if k.startswith(('VIDEO_USE_MODEL','VIDEO_USE_CONTROL','VIDEO_USE_JEV')) or k=='VIDEO_USE_INTERNAL_PROVIDER_HOSTS'}
 with suite_lock(),tempfile.TemporaryDirectory(prefix='vseed-',dir='/vqt') as folder:
  root=Path(folder);root.chmod(0o755);isolate_environment(root,root/'rt');os.environ.update(model)
  pg=PostgresServer();s3=S3Server();port=free_port();host=None;server=None;worker=None;api_thread=None
  try:
   os.environ.update(VIDEO_USE_TELEMETRY_ENABLED='1',VIDEO_USE_DATABASE_URL=pg.dsn,VIDEO_USE_OBJECT_ENDPOINT=s3.endpoint,VIDEO_USE_OBJECT_BUCKET='seed-qa',AWS_ACCESS_KEY_ID='test',AWS_SECRET_ACCESS_KEY='test',AWS_DEFAULT_REGION='us-east-1',VIDEO_USE_DISTRIBUTED='1',VIDEO_USE_SESSION_SECRET=__import__('secrets').token_hex(32),VIDEO_USE_UID_START='850000',VIDEO_USE_UID_COUNT='8',VIDEO_USE_CONCURRENCY='1',VIDEO_USE_WORKSPACE_STORAGE='tmpfs')
   from webapp import runtime_image
   runtime_image.MANIFEST=root/'rt/manifest.json';runtime_image.MANIFEST.parent.mkdir(mode=0o755);shutil.copyfile('runtime/build/manifest.json',runtime_image.MANIFEST);runtime_image.stage_tools(runtime_image.MANIFEST.parent)
   from webapp.artifact_store import S3Objects
   S3Objects('seed-qa',s3.endpoint).client.create_bucket(Bucket='seed-qa')
   from webapp.agent_host import AgentHost
   from webapp.demo_library import DemoLibrary
   from webapp.app import create_app
   import uvicorn
   host=AgentHost(root/'data');host.socket_dir=root/'sockets';host.socket_dir.mkdir();host.workflow_runner.adapter.socket_dir=host.socket_dir
   source_library(root/'library');DemoLibrary(host.store).publish(root/'library')
   host.coordinator.heartbeat();worker=threading.Thread(target=host.run,daemon=True);worker.start()
   server=uvicorn.Server(uvicorn.Config(create_app(root/'data',False),host='127.0.0.1',port=port,log_level='warning'));api_thread=threading.Thread(target=server.run,daemon=True);api_thread.start()
   while not server.started:time.sleep(.05)
   result=subprocess.run(['node','tests/campaign_seed_confirmation.cjs',f'http://127.0.0.1:{port}',str(out)],timeout=900)
   host.draining.set();worker.join(45)
   if worker.is_alive():host.stop.set();worker.join(30)
   if worker.is_alive():raise RuntimeError('Isolated worker did not stop')
   from webapp.task_usage import snapshot
   from webapp.workspace_state import seed_manifest
   evidence={'passed':result.returncode==0,'isolation':{'postgres_port':pg.port,'api_port':port,'production_changed':False},'tasks':[]}
   for task in host.store.query('SELECT id,project,mode,status,plan_id FROM agent_turns ORDER BY created'):
    task['usage']=snapshot(host.store,task['id'],scope='project',limit=200);task['attempts']=host.store.query('SELECT id,generation,kind,status,runtime FROM execution_attempts WHERE run_id=?',(task['id'],));task['readiness']=host.store.query('SELECT * FROM plan_readiness WHERE run_id=?',(task['id'],));evidence['tasks'].append(task)
   state_path=out/'state.json'
   if state_path.exists():
    state=json.loads(state_path.read_text());p=state['project'];evidence['seed']=seed_manifest(host.store,p);evidence['checkpoints']=host.store.query('SELECT attempt,manifest FROM execution_checkpoint_versions WHERE project=? ORDER BY created',(p,))
   (out/'evidence.json').write_text(json.dumps(evidence,ensure_ascii=False,indent=2));print(json.dumps({'passed':evidence['passed'],'tasks':len(evidence['tasks'])}),flush=True);assert evidence['passed']
  finally:
   if host and worker and worker.is_alive():host.stop.set();host.draining.set();worker.join(30)
   if server:server.should_exit=True
   if api_thread:api_thread.join(10)
   s3.close();pg.close()

if __name__=='__main__':main()
