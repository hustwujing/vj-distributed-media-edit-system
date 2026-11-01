import argparse
import contextlib
import io
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from tests.isolation import temporary_root as isolated_tmp_root

from PIL import Image
from fastapi.testclient import TestClient

from webapp.app import create_app

sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'helpers'))
from image_edit import ImagePlanError, render_plan, validate
from preview_style import create_preview


class ImageEditingTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory(prefix='image-edit-',dir=isolated_tmp_root())
        self.root=Path(self.temp.name);(self.root/'sources').mkdir();(self.root/'edit').mkdir()
        image=Image.new('RGBA',(320,200),(30,60,90,255));image.save(self.root/'sources/main.png')
        logo=Image.new('RGBA',(40,40),(220,40,40,180));logo.save(self.root/'sources/logo.png')
        self.plan={'version':1,'sources':{'main':'sources/main.png','logo':'sources/logo.png'},'operations':[
            {'type':'resize','width':640,'height':400},{'type':'adjust','brightness':1.1,'contrast':1.05},
            {'type':'text','text':'图片工具验证','x':20,'y':20,'size':32,'color':'#FFFFFF'},
            {'type':'overlay','source':'sources/logo.png','x':580,'y':20,'opacity':.8}], 'output':{'format':'png'}}

    def tearDown(self):self.temp.cleanup()

    def test_plan_renders_new_image_and_preserves_source(self):
        p=validate(self.plan,self.root);self.assertEqual(p['_format'],'png')
        with contextlib.redirect_stdout(io.StringIO()):report=render_plan(p,Path('edit/result.png'))
        self.assertEqual((report['width'],report['height']),(640,400))
        with Image.open(self.root/'edit/result.png') as image:self.assertEqual(image.size,(640,400))
        with Image.open(self.root/'sources/main.png') as image:self.assertEqual(image.size,(320,200))

    def test_image_plan_rejects_paths_and_overwrites(self):
        bad=json.loads(json.dumps(self.plan));bad['sources']['main']='/etc/passwd'
        with self.assertRaises(ImagePlanError):validate(bad,self.root)
        bad=json.loads(json.dumps(self.plan));bad['operations']=[{'type':'overlay','source':'/etc/passwd'}]
        with self.assertRaises(ImagePlanError):validate(bad,self.root)
        out=self.root/'edit/existing.png';out.write_bytes(b'old')
        with self.assertRaises(ImagePlanError):render_plan(validate(self.plan,self.root),Path('edit/existing.png'))
        self.assertEqual(out.read_bytes(),b'old')

    def test_style_preview_accepts_image_source(self):
        args=argparse.Namespace(workspace=self.root,output=self.root/'edit/previews/turn/style.png',width=320,height=200,input='sources/main.png',time=0,
                                 text='图片预览',font_size=24,color='#FFFFFF',position='bottom',no_box=False,fit='contain')
        result=create_preview(args);self.assertTrue(result['preview_only'])
        with Image.open(args.output) as image:self.assertEqual(image.size,(320,200))

    def test_web_uploads_image_and_keeps_project_isolation(self):
        app=create_app(self.root/'web',run_worker=False)
        with TestClient(app) as client, TestClient(app) as other:
            project=client.post('/api/projects',json={'name':'图片项目'}).json()['id']
            response=client.post(f'/api/projects/{project}/assets',files={'file':('main.png',(self.root/'sources/main.png').read_bytes(),'image/png')})
            self.assertEqual(response.status_code,201,response.text);asset=response.json()
            self.assertEqual(asset['kind'],'image');self.assertEqual((asset['width'],asset['height']),(320,200))
            data=client.get(f'/api/projects/{project}').json()['assets'][0];self.assertEqual(data['kind'],'image')
            self.assertEqual(client.get('/'+data['url']).status_code,200)
            self.assertEqual(client.get('/'+data['poster_url']).status_code,200)
            self.assertEqual(other.get('/'+data['url']).status_code,404)


if __name__=='__main__':unittest.main()
