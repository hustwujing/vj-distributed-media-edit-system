"""Load the actual public 44-case catalogue using isolated PG/S3/API nodes."""
import hashlib,json,os,shutil,tempfile
from pathlib import Path
from tests.suite_lock import suite_lock
from tests.distributed_support import PostgresServer,S3Server
from tests.isolation import isolate_environment,isolate_network


def main():
 source=Path('workspace/demos').resolve();output=Path('.cluster/campaigns/20261101-stream-activity/demo-isolated.json').resolve()
 with suite_lock(),tempfile.TemporaryDirectory(prefix='vdl-',dir='/vqt') as folder:
  root=Path(folder);root.chmod(0o755);isolate_environment(root);isolate_network();pg=PostgresServer();s3=S3Server()
  try:
   os.environ.update(VIDEO_USE_DATABASE_URL=pg.dsn,VIDEO_USE_OBJECT_ENDPOINT=s3.endpoint,VIDEO_USE_OBJECT_BUCKET='actual-demos',AWS_ACCESS_KEY_ID='test',AWS_SECRET_ACCESS_KEY='test',AWS_DEFAULT_REGION='us-east-1',VIDEO_USE_DISTRIBUTED='1',VIDEO_USE_SESSION_SECRET='d3'*32)
   from webapp.artifact_store import S3Objects
   from webapp.demo_library import DemoLibrary
   from webapp.app import create_app
   from fastapi.testclient import TestClient
   S3Objects('actual-demos',s3.endpoint).client.create_bucket(Bucket='actual-demos')
   a=create_app(root/'a',False);b=create_app(root/'b',False);publication=DemoLibrary(a.state.store).publish(source);catalog=DemoLibrary(b.state.store).snapshot().catalog
   results=[]
   with TestClient(a) as first,TestClient(b) as second:
    assert first.get('/api/demos').json()==second.get('/api/demos').json()==catalog
    for demo in catalog:
     node=first if len(results)%2==0 else second
     preview=node.get(f"/api/demos/{demo['id']}/files/{demo['poster']}");assert preview.status_code==200,(demo['id'],preview.text[:300])
     assert hashlib.sha256(preview.content).digest()==hashlib.sha256((source/demo['id']/demo['poster']).read_bytes()).digest()
     loaded=node.post(f"/api/demos/{demo['id']}/load");assert loaded.status_code==201,(demo['id'],loaded.text[:1000])
     project=loaded.json()['project']['id'];peer=second if node is first else first;peer.cookies.update(node.cookies)
     data=peer.get('/api/projects/'+project);assert data.status_code==200
     for asset in data.json()['assets']:
      response=peer.get('/'+asset['url']);assert response.status_code==200,(demo['id'],asset['name'])
     results.append({'id':demo['id'],'passed':True,'assets':len(data.json()['assets'])});print(demo['id'],'passed',flush=True)
   report={'passed':True,'publication':publication,'cases':results};output.write_text(json.dumps(report,ensure_ascii=False,indent=2)+'\n');print(json.dumps({'passed':True,'examples':len(results)}))
  finally:s3.close();pg.close()

if __name__=='__main__':main()
