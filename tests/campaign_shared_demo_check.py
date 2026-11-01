import concurrent.futures,json,subprocess,hashlib
from pathlib import Path
from deploy import cluster
release=json.loads(Path('.cluster/release.json').read_text())
def check(node):
 env={**cluster.environment(node),**cluster.env_file(cluster.CONF/f'api-{node}.env'),'PYTHONPATH':release['source'],'VIDEO_USE_TELEMETRY_STREAM':'stderr'}
 p=subprocess.run(['runuser','-u','video-use','--',str(Path(release['source'])/'.venv/bin/python'),'/tmp/vu-verify-shared-demos.py'],env=env,cwd=release['source'],capture_output=True,text=True,check=True,timeout=300)
 result=json.loads(p.stdout);result['node']=node
 for f in result['files']:
  with (Path('workspace/demos')/f['name']).open('rb') as stream:assert hashlib.file_digest(stream,'sha256').hexdigest()==f['sha256']
 return result
with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:results=list(pool.map(check,['a','b']))
root=Path('.cluster/campaigns/20261101-combined');(root/'shared-storage-check.json').write_text(json.dumps({'passed':True,'nodes':results},indent=2)+'\n');print(json.dumps({'passed':True,'nodes':len(results),'files_per_node':[len(r['files']) for r in results]}))
