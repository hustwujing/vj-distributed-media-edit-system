"""Isolated PostgreSQL/Redis/API + actual Agent; never change deployed services."""
import json,os,shutil,subprocess,sys,tempfile,threading,time
from pathlib import Path
from tests.suite_lock import suite_lock
from tests.distributed_support import PostgresServer,S3Server,free_port
from tests.isolation import isolate_environment
from deploy.dynamic_cluster import environment


def main():
 output=Path('.cluster/campaigns/20261101-experience').resolve();output.mkdir(mode=0o700,parents=True,exist_ok=True)
 configured=environment();model={k:v for k,v in configured.items() if k.startswith(('VIDEO_USE_MODEL','VIDEO_USE_CONTROL','VIDEO_USE_JEV')) or k=='VIDEO_USE_INTERNAL_PROVIDER_HOSTS'}
 with suite_lock(),tempfile.TemporaryDirectory(prefix='vsa-',dir='/vqt') as folder:
  root=Path(folder);root.chmod(0o755);isolate_environment(root,root/'rt');os.environ.update(model)
  pg=PostgresServer();s3=S3Server();redis_port=free_port();api_port=free_port();redis_process=None;host=None;server=None;worker=None;fixture_stop=threading.Event()
  def start_redis():
   return subprocess.Popen(['redis-server','--bind','127.0.0.1','--port',str(redis_port),'--save','','--appendonly','no','--dir',str(root)],stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
  try:
   os.environ.update(VIDEO_USE_TELEMETRY_ENABLED='1',VIDEO_USE_DATABASE_URL=pg.dsn,VIDEO_USE_OBJECT_ENDPOINT=s3.endpoint,VIDEO_USE_OBJECT_BUCKET='stream-qa',AWS_ACCESS_KEY_ID='test',AWS_SECRET_ACCESS_KEY='test',AWS_DEFAULT_REGION='us-east-1',VIDEO_USE_DISTRIBUTED='1',VIDEO_USE_SESSION_SECRET=__import__('secrets').token_hex(32),VIDEO_USE_REDIS_URL=f'redis://127.0.0.1:{redis_port}/0',VIDEO_USE_AGENT_TEXT_SNAPSHOT_SECONDS='5',VIDEO_USE_UID_START='850000',VIDEO_USE_UID_COUNT='8',VIDEO_USE_CONCURRENCY='1',VIDEO_USE_WORKSPACE_STORAGE='tmpfs')
   from webapp import runtime_image
   runtime_image.MANIFEST=root/'rt/manifest.json';runtime_image.MANIFEST.parent.mkdir(mode=0o755)
   shutil.copyfile('runtime/build/manifest.json',runtime_image.MANIFEST);runtime_image.stage_tools(runtime_image.MANIFEST.parent)
   from webapp.artifact_store import S3Objects
   S3Objects('stream-qa',s3.endpoint).client.create_bucket(Bucket='stream-qa')
   redis_process=start_redis();time.sleep(.2)
   from webapp.agent_host import AgentHost
   from webapp import agent_store
   from webapp.agent_text import TextPublisher
   from webapp.live_activity import span
   from webapp.agent_stream import bus
   host=AgentHost(root/'data');host.socket_dir=root/'sockets';host.socket_dir.mkdir();host.workflow_runner.adapter.socket_dir=host.socket_dir
   host.coordinator.heartbeat()
   from webapp.app import create_app
   import uvicorn
   server=uvicorn.Server(uvicorn.Config(create_app(root/'data',False),host='127.0.0.1',port=api_port,log_level='warning'))
   api_thread=threading.Thread(target=server.run,daemon=True);api_thread.start()
   while not server.started:time.sleep(.05)
   evidence={'isolation':{'postgres_port':pg.port,'redis_port':redis_port,'api_port':api_port,'production_changed':False},'fixtures':[]}
   from PIL import Image,ImageDraw
   im=Image.new('RGB',(80,40),'black');ImageDraw.Draw(im).rectangle((10,8,70,32),fill='yellow');im.save(output/'logo.png')
   subprocess.run(['ffmpeg','-v','error','-f','lavfi','-i','color=c=blue:s=320x180:d=2:r=12','-c:v','libx264','-pix_fmt','yuv420p','-y',str(output/'clip.mp4')],check=True)
   full='我现在检查 BiRefNet 产出的实际前景图和蒙版，重点核对主体完整性、透明背景及边缘黑边；随后会提交结构化验收结论。'
   def fixtures():
    nonlocal redis_process,worker
    try:
     last_heartbeat=0.
     while not fixture_stop.wait(.05):
      if worker is None and time.monotonic()-last_heartbeat>1:
       host.coordinator.heartbeat();last_heartbeat=time.monotonic()
      control=output/'control.json'
      if not control.exists():continue
      data=json.loads(control.read_text());control.unlink()
      if data['action']=='live':
       redis_process=start_redis();time.sleep(.2);bus()._client=None;bus()._retry_after=0
       worker=threading.Thread(target=host.run,daemon=True);worker.start();continue
      task=data['task'];lease=host.coordinator.claim(task,'agent');assert lease
      try:
       with lease.bind():
        host.store.execute("UPDATE agent_turns SET status='running' WHERE id=?",(task,));publisher=TextPublisher(host.store,task)
        publisher.message('progress','我');time.sleep(.2);publisher.message('progress',full[:39]);time.sleep(.2)
        if data['action']=='database':redis_process.terminate();redis_process.wait(timeout=5)
        publisher.message('progress',full,complete=True);completed=time.time()
        with span('host','operation','等待测试步骤结束','fixture.wait'):time.sleep(2)
        publisher.message('final','最终检查通过。','final_answer',complete=True)
        agent_store.finish(host.store,task,'succeeded','最终检查通过。')
        evidence['fixtures'].append({'task':task,'transport':data['action'],'text_complete_at':completed,'finished_at':time.time()})
      finally:lease.release()
    except Exception as exc:evidence['fixture_error']=str(exc);fixture_stop.set()
   fixture_thread=threading.Thread(target=fixtures,daemon=True);fixture_thread.start()
   result=subprocess.run(['node','tests/campaign_experience.cjs',f'http://127.0.0.1:{api_port}',str(output)],timeout=1500)
   fixture_stop.set();fixture_thread.join(timeout=10)
   if worker:
    host.draining.set();worker.join(timeout=45)
    if worker.is_alive():host.stop.set();worker.join(timeout=15)
    if worker.is_alive():raise RuntimeError('Isolated worker did not stop')
   for task in host.store.query('SELECT id,status,mode FROM agent_turns'):
    task['attempts']=host.store.query('SELECT id,status,runtime,uid FROM execution_attempts WHERE run_id=?',(task['id'],))
    from webapp.task_usage import snapshot
    task['usage']=snapshot(host.store,task['id'],limit=200)
    task['render_observations']=host.store.query('SELECT * FROM agent_render_observations WHERE task_id=?',(task['id'],))
    task['events']=host.store.query('SELECT kind,payload,created FROM agent_events WHERE run_id=? ORDER BY seq',(task['id'],));evidence.setdefault('tasks',[]).append(task)
   evidence['passed']=result.returncode==0 and 'fixture_error' not in evidence
   (output/'isolated-evidence.json').write_text(json.dumps(evidence,ensure_ascii=False,indent=2)+'\n')
   print(json.dumps({'passed':evidence['passed'],'fixture_cases':len(evidence['fixtures']),'tasks':len(evidence.get('tasks',[]))}),flush=True)
   assert evidence['passed']
  finally:
   fixture_stop.set()
   if host and worker and worker.is_alive():host.stop.set();host.draining.set();worker.join(timeout=30)
   if server:server.should_exit=True
   if redis_process and redis_process.poll() is None:redis_process.terminate();redis_process.wait(timeout=5)
   s3.close();pg.close()

if __name__=='__main__':main()
