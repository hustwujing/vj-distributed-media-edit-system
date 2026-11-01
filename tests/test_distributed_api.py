import io
import base64
import os
from pathlib import Path
import shutil
import tempfile
import unittest
from unittest.mock import patch
from fastapi.testclient import TestClient
from PIL import Image
from tests.distributed_support import PostgresServer,S3Server
from webapp.artifact_store import S3Objects
from webapp.app import create_app


class DistributedApiTests(unittest.TestCase):
    def test_shared_session_and_asset_download_without_original_api_disk(self):
        pg=PostgresServer();s3=S3Server()
        try:
            with tempfile.TemporaryDirectory() as folder,patch.dict(os.environ,{
                'VIDEO_USE_DATABASE_URL':pg.dsn,'VIDEO_USE_OBJECT_ENDPOINT':s3.endpoint,'VIDEO_USE_OBJECT_BUCKET':'api-files',
                'AWS_ACCESS_KEY_ID':'test','AWS_SECRET_ACCESS_KEY':'test','AWS_DEFAULT_REGION':'us-east-1',
                'VIDEO_USE_SESSION_SECRET':'a1'*32,'VIDEO_USE_DISTRIBUTED':'1',
                'VIDEO_USE_OBJECT_ENCRYPTION_KEY':base64.b64encode(b'test encryption key 32 bytes!!!!').decode(),
                'VIDEO_USE_OBJECT_REQUIRE_ENCRYPTION':'1',
            }):
                backend=S3Objects('api-files',s3.endpoint);backend.client.create_bucket(Bucket='api-files')
                root=Path(folder);a=create_app(root/'a',run_worker=False);b=create_app(root/'b',run_worker=False)
                with TestClient(a) as first,TestClient(b) as second,TestClient(b) as stranger:
                    response=first.post('/api/projects',json={'name':'shared project'})
                    self.assertEqual(response.status_code,201,response.text)
                    project=response.json()['id'];second.cookies.update(first.cookies)
                    image=io.BytesIO();Image.new('RGB',(16,16),'blue').save(image,format='PNG')
                    response=first.post(f'/api/projects/{project}/assets',files={'file':('blue.png',image.getvalue(),'image/png')})
                    self.assertEqual(response.status_code,201,response.text)
                    asset=response.json()['id']
                    filename=a.state.store.query('SELECT filename FROM assets WHERE id=?',(asset,))[0]['filename']
                    record=a.state.store.artifacts.resolve(project,'sources/'+filename)
                    stored=backend.client.get_object(Bucket=backend.bucket,Key=record['object_key'])
                    try:self.assertTrue(stored['Body'].read().startswith(b'VUOBJ01\0'))
                    finally:stored['Body'].close()
                    shutil.rmtree(root/'a')
                    response=second.get(f'/api/projects/{project}/assets/{asset}/file')
                    self.assertEqual(response.status_code,200,response.text)
                    self.assertEqual(response.content,image.getvalue())
                    self.assertEqual(stranger.get(f'/api/projects/{project}/assets/{asset}/file').status_code,404)
                    self.assertEqual(second.get(f'/api/projects/{project}').status_code,200)
        finally:s3.close();pg.close()


if __name__=='__main__':unittest.main()
