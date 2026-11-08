import contextlib
import io
import json
import tempfile
import unittest
import zipfile
from pathlib import Path
from tests.isolation import temporary_root as isolated_tmp_root
from unittest.mock import patch

import numpy as np
from PIL import Image,ImageDraw
from fastapi.testclient import TestClient
from webapp.app import create_app
from webapp.image_tasks import execute_image
from webapp.image_document import Document,export_document

class ImageWorkbenchTests(unittest.TestCase):
    def test_automatic_adjustment_preserves_flat_color_alpha_and_temperature_intent(self):
        project=self.app.state.store.project_dir(self.pid);assets=self.app.state.store.assets(self.pid)
        source=project/'sources'/assets[0]['filename']
        flat=np.zeros((80,120,4),dtype='uint8');flat[:]=[40,100,60,255]
        flat[:10]=[250,0,200,0];Image.fromarray(flat,'RGBA').save(source)
        output=self.root/'auto-flat'
        execute_image({'action':'local_adjust','asset':self.aid,'parameters':{'auto_levels':True,'auto_white_balance':True}},assets,project,output)
        with Image.open(output/'image.png') as image:actual=np.asarray(image)
        np.testing.assert_array_equal(actual[10:,:,:3],flat[10:,:,:3]);np.testing.assert_array_equal(actual[...,3],flat[...,3])
        output=self.root/'temperature-only'
        execute_image({'action':'local_adjust','asset':self.aid,'parameters':{'temperature':.2}},assets,project,output)
        with Image.open(output/'image.png') as image:pixel=image.getpixel((30,30))
        self.assertEqual(pixel,(49,100,51,255))

    def test_auto_levels_do_not_destroy_colored_subject_on_neutral_background(self):
        project=self.app.state.store.project_dir(self.pid);assets=self.app.state.store.assets(self.pid)
        image=Image.new('RGBA',(120,80),(230,230,230,255));ImageDraw.Draw(image).rectangle((30,20,90,60),fill=(40,100,60,255))
        image.save(project/'sources'/assets[0]['filename']);output=self.root/'auto-subject'
        execute_image({'action':'local_adjust','asset':self.aid,'parameters':{'auto_levels':True,'auto_white_balance':True,'gamma':1.1}},assets,project,output)
        with Image.open(output/'image.png') as result:
            red,green,blue,alpha=result.getpixel((60,40))
        self.assertGreater(green,70);self.assertGreater(green,red+30);self.assertGreater(green,blue+20);self.assertEqual(alpha,255)

    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory(dir=isolated_tmp_root());self.root=Path(self.tmp.name)
        self.app=create_app(self.root,run_worker=False);self.a=TestClient(self.app);self.b=TestClient(self.app)
        self.a.__enter__();self.b.__enter__();self.pid=self.a.post('/api/projects',json={'name':'画布'}).json()['id']
        image=Image.new('RGB',(120,80),'#385939');b=io.BytesIO();image.save(b,format='PNG')
        self.aid=self.a.post(f'/api/projects/{self.pid}/assets',files={'file':('original.png',b.getvalue(),'image/png')}).json()['id']
        self.doc={'revision':0,'width':120,'height':80,'background':'transparent','layers':[
            {'id':'original','kind':'image','asset':self.aid,'width':120,'height':80,'name':'原图'},
            {'id':'box','kind':'rectangle','x':10,'y':10,'width':20,'height':20,'color':'#ff0000','name':'色块'}]}
    def tearDown(self):self.b.__exit__(None,None,None);self.a.__exit__(None,None,None);self.tmp.cleanup()
    def test_canvas_revisions_conflicts_restore_and_owner(self):
        path=f'/api/projects/{self.pid}/image-document'
        r=self.a.put(path,json=self.doc);self.assertEqual(r.status_code,200,r.text)
        self.assertEqual(self.a.put(path,json=self.doc).status_code,409)
        body={**self.doc,'revision':1};body['width']=240
        self.assertEqual(self.a.put(path,json=body).status_code,200)
        self.assertEqual(self.a.get(path+'/1').json()['width'],120)
        self.assertEqual(len(self.a.get(path).json()['versions']),2)
        self.assertEqual(self.b.get(path).status_code,404);self.assertEqual(self.b.put(path,json=body).status_code,404)
        self.assertEqual(self.b.get('/images/'+self.pid).status_code,404)
    def test_layer_refs_and_image_task_paths_cannot_escape_project(self):
        other=self.b.post('/api/projects',json={'name':'other'}).json()['id']
        self.assertEqual(self.b.put(f'/api/projects/{other}/image-document',json=self.doc).status_code,422)
        for body in [{'action':'remove_bg','asset':self.aid},{'action':'generate','asset':'/etc/passwd','prompt':'x'},
                     {'action':'remove_bg','asset':'no','parameters':{'path':'/etc/passwd'}}]:
            self.assertIn(self.b.post(f'/api/projects/{other}/image-jobs',json=body).status_code,[422,404])
        for body in [{'action':'segment','asset':self.aid},{'action':'erase','asset':self.aid,'box':[0,0,300,300]}]:
            self.assertEqual(self.a.post(f'/api/projects/{self.pid}/image-jobs',json=body).status_code,422)
        for body in [{'action':'perspective','asset':self.aid,'points':[[0,0]]},{'action':'local_adjust','asset':self.aid,'parameters':{'brightness':99}},
                     {'action':'product_layout','asset':self.aid,'parameters':{'width':1,'height':100,'background':'bad'}},
                     {'action':'color_match','asset':self.aid,'references':[],'parameters':{'strength':1}},
                     {'action':'segment_sam3','asset':self.aid},{'action':'segment_sam3','asset':self.aid,'prompt':'咖啡杯'},{'action':'refine_zim','asset':self.aid}]:
            self.assertEqual(self.a.post(f'/api/projects/{self.pid}/image-jobs',json=body).status_code,422)
        self.assertEqual(self.b.post(f'/api/projects/{other}/image-batch',json={'assets':[self.aid],'width':100,'height':100}).status_code,422)
    def test_sam3_rejects_ignored_spatial_constraints_before_queuing(self):
        from webapp.image_document import ImageTask,check_task
        from webapp.operations import validate_parameters,get
        assets={a['id']:a for a in self.app.state.store.assets(self.pid)}
        inputs={'source':{'asset':self.aid}}
        for selection in ({'prompt':'person','box':[0,0,100,70]},{'prompt':'person','points':[[20,20]]},
                          {'box':[0,0,100,70],'points':[[20,20]]}):
            body={'action':'segment_sam3','asset':self.aid,**selection}
            reply=self.a.post(f'/api/projects/{self.pid}/image-jobs',json=body)
            self.assertEqual(reply.status_code,422)
            with self.assertRaisesRegex(ValueError,'不能同时使用'):
                validate_parameters('image.segment_sam3',selection,inputs)
        self.assertEqual(self.a.get(f'/api/projects/{self.pid}').json()['jobs'],[])
        for values in ({'prompt':' person '},{'prompt':'  ','points':[[20,20]]},{'box':[0,0,100,70]}):
            task=ImageTask.model_validate({'action':'segment_sam3','asset':self.aid,**values})
            check_task(task,assets)
            self.assertEqual(sum([bool(task.prompt),bool(task.points),task.box is not None]),1)
        self.assertFalse(get('image.segment_sam3').public()['constraints']['text_and_spatial_combination'])

    def test_psd_ora_and_composite_preserve_layer_order(self):
        from psd_tools import PSDImage
        assets={a['id']:a for a in self.app.state.store.assets(self.pid)};project=self.app.state.store.project_dir(self.pid)
        for fmt in ('png','psd','ora'):
            out=self.root/fmt;out.mkdir();files=export_document(Document.model_validate(self.doc),assets,project,out,fmt)
            with Image.open(out/'image.png') as im:self.assertEqual(im.getpixel((15,15)),(255,0,0,255))
            if fmt=='psd':
                psd=PSDImage.open(out/'editable.psd');self.assertEqual(len(psd),2);self.assertEqual(psd[-1].name,'色块')
            if fmt=='ora':
                with zipfile.ZipFile(out/'editable.ora') as archive:self.assertIn('data/1.png',archive.namelist());self.assertIn(b'svg:src-over',archive.read('stack.xml'))
    def test_masked_generation_preserves_every_pixel_outside_selection(self):
        store=self.app.state.store;project=store.project_dir(self.pid);output=project/'edit/test';output.mkdir()
        fake=self.root/'generated.png';Image.new('RGBA',(60,40),'red').save(fake)
        with patch('webapp.image_tasks.remote_images',return_value=[{'name':'edited.png','path':fake,'kind':'image','type':'image/png'}]):
            execute_image({'action':'local_edit','asset':self.aid,'box':[10,10,30,20],'prompt':'red'},store.assets(self.pid),project,output)
        image=np.asarray(Image.open(output/'image.png'));source=np.asarray(Image.open(project/'sources'/store.assets(self.pid)[0]['filename']).convert('RGBA'))
        mask=np.zeros((80,120),bool);mask[10:30,10:40]=True
        self.assertTrue(np.array_equal(image[~mask],source[~mask]));self.assertEqual(image[15,15,0],255)
    def test_authorized_result_import_becomes_own_editable_asset(self):
        store=self.app.state.store;job=store.add_job(self.pid,'image',{'tool':'remove_bg'});out=store.project_dir(self.pid)/'edit/jobs'/job['id'];out.mkdir(parents=True)
        Image.new('RGBA',(64,64),'red').save(out/'foreground.png');store.finish(job['id'],'succeeded',['foreground.png'])
        endpoint=f'/api/projects/{self.pid}/image-import';body={'job':job['id'],'names':['foreground.png']}
        self.assertEqual(self.b.post(endpoint,json=body).status_code,404)
        result=self.a.post(endpoint,json=body);self.assertEqual(result.status_code,201,result.text);self.assertEqual(result.json()['assets'][0]['width'],64)
        self.assertEqual(self.a.post(endpoint,json={'job':job['id'],'names':['../../secret.png']}).status_code,404)

    def test_erase_forwards_target_instruction_and_preserves_outside_pixels(self):
        from webapp.operations import validate_parameters
        store=self.app.state.store;project=store.project_dir(self.pid);output=project/'edit/erase';output.mkdir()
        prompt='Remove the SALE text and restore the blank white background.'
        parameters={'box':[10,10,30,20],'prompt':prompt,'quality':'high'}
        validate_parameters('image.erase',parameters,{'source':{'asset':self.aid}})
        fake=self.root/'erased.png';Image.new('RGB',(60,40),'white').save(fake)
        with patch('webapp.image_tasks.remote_images',return_value=[{'name':'edited.png','path':fake,'kind':'image','type':'image/png'}]) as call:
            execute_image({'action':'erase','asset':self.aid,**parameters},store.assets(self.pid),project,output)
        sent=call.call_args.args[3]
        self.assertIn(prompt,sent['prompt']);self.assertIn('图2',sent['prompt']);self.assertEqual(sent['quality'],'high')
        self.assertEqual(len(call.call_args.args[4]),1)
        image=np.asarray(Image.open(output/'image.png'));source=np.asarray(Image.open(project/'sources'/store.assets(self.pid)[0]['filename']).convert('RGBA'))
        mask=np.zeros((80,120),bool);mask[10:30,10:40]=True
        self.assertTrue(np.array_equal(image[~mask],source[~mask]))
        self.assertTrue(np.all(image[mask,:3]==255))
    def test_agent_image_gateway_respects_mode_and_rejects_foreign_asset(self):
        from unittest.mock import Mock
        from webapp.agent_gateway import Gateway
        g=object.__new__(Gateway);g.mode='plan';g.assets={}
        self.assertEqual(g.image_result({'target_asset':self.aid,'prompt':'x'}).status,403)
        g.mode='execute';self.assertEqual(g.image_result({'target_asset':'foreign','prompt':'x'}).status,422)
        g.image_calls=0;g.local_image_calls=0
        for _ in range(4):g.count_image_call('generate')
        with self.assertRaisesRegex(RuntimeError,'4次'):g.count_image_call('generate')
        for _ in range(20):g.count_image_call('local_adjust')
        with self.assertRaisesRegex(RuntimeError,'20次'):g.count_image_call('perspective')

    def test_model_free_mask_perspective_adjust_match_product_and_batch(self):
        store=self.app.state.store;project=store.project_dir(self.pid);assets=store.assets(self.pid)
        mask=Image.new('L',(120,80));d=ImageDraw.Draw(mask);d.rectangle((20,10,90,70),fill=255);d.rectangle((40,30,50,40),fill=0);buf=io.BytesIO();mask.save(buf,format='PNG')
        mask_id=self.a.post(f'/api/projects/{self.pid}/assets',files={'file':('mask.png',buf.getvalue(),'image/png')}).json()['id'];assets=store.assets(self.pid)
        cases=[
          ({'action':'refine_mask','asset':mask_id,'parameters':{'fill_holes':True,'grow':2,'feather':1,'threshold':128,'remove_small':2}},'mask.png'),
          ({'action':'perspective','asset':self.aid,'points':[[0,0],[119,0],[119,79],[0,79]]},'image.png'),
          ({'action':'local_adjust','asset':self.aid,'parameters':{'brightness':1.3,'contrast':1.1,'saturation':.5,'temperature':.2,'sharpen':.5,'blur':0,'auto_white_balance':True,'auto_levels':True}},'image.png'),
          ({'action':'color_match','asset':self.aid,'references':[mask_id],'parameters':{'strength':.5}},'image.png'),
          ({'action':'product_layout','asset':self.aid,'parameters':{'width':200,'height':200,'background':'#ffffff','margin':20,'shadow':True,'shadow_blur':5}},'image.png'),
          ({'action':'batch_export','asset':self.aid,'parameters':{'width':160,'height':160,'fit':'contain','background':'#ffffff','format':'webp','quality':80,'watermark':'品牌'}},'image.webp')]
        for i,(task,name) in enumerate(cases):
            out=project/f'edit/local-{i}';files=execute_image(task,assets,project,out);self.assertIn(name,files);self.assertTrue((out/name).is_file())
        with Image.open(project/'edit/local-4/image.png') as im:self.assertEqual(im.size,(200,200))
        with Image.open(project/'edit/local-5/image.webp') as im:self.assertEqual(im.size,(160,160))
        batch=self.a.post(f'/api/projects/{self.pid}/image-batch',json={'assets':[self.aid,mask_id],'width':128,'height':128,'format':'png','watermark':'测试'})
        self.assertEqual(batch.status_code,202,batch.text);self.assertEqual(len(batch.json()['jobs']),2)

    def test_batch_contain_preserves_same_size_pixels_and_full_available_area(self):
        store=self.app.state.store;project=store.project_dir(self.pid);assets=store.assets(self.pid)
        source=Image.new('RGB',(120,80),'#385939')
        ImageDraw.Draw(source).rectangle((0,0,119,4),fill='red')
        ImageDraw.Draw(source).rectangle((55,74,65,79),fill='blue')
        source.save(project/'sources'/next(a['filename'] for a in assets if a['id']==self.aid))
        for width,height,fit in ((120,80,None),(240,160,'contain'),(120,120,'contain')):
            with self.subTest(width=width,height=height,fit=fit):
                parameters={'width':width,'height':height,'format':'png'}
                if fit:parameters['fit']=fit
                out=project/f'edit/contain-{width}-{height}'
                execute_image({'action':'batch_export','asset':self.aid,'parameters':parameters},assets,project,out)
                actual=np.asarray(Image.open(out/'image.png').convert('RGB'))
                if (width,height)==(120,80):self.assertTrue(np.array_equal(actual,np.asarray(source)))
                elif width==240:
                    self.assertTrue(np.array_equal(actual,np.asarray(source.resize((240,160),Image.Resampling.LANCZOS))))
                else:
                    self.assertTrue(np.array_equal(actual[20:100],np.asarray(source)))
                    self.assertTrue(np.all(actual[:20]==255));self.assertTrue(np.all(actual[100:]==255))

    def test_batch_cover_and_product_layout_keep_distinct_geometry(self):
        store=self.app.state.store;project=store.project_dir(self.pid);assets=store.assets(self.pid)
        for action,parameters,expected_box in (
            ('batch_export',{'width':120,'height':120,'fit':'cover'},(0,0,120,120)),
            ('product_layout',{'width':120,'height':80,'shadow':False},(9,6,111,74)),
            ('product_layout',{'width':120,'height':80,'margin':10,'shadow':False},(15,10,105,70))):
            with self.subTest(action=action,parameters=parameters):
                out=project/f'edit/geometry-{action}-{parameters.get("margin",0)}'
                execute_image({'action':action,'asset':self.aid,'parameters':parameters},assets,project,out)
                a=np.asarray(Image.open(out/'image.png').convert('RGB'));ys,xs=np.where(np.any(a!=255,axis=2))
                self.assertEqual((int(xs.min()),int(ys.min()),int(xs.max()+1),int(ys.max()+1)),expected_box)

    def test_nondestructive_adjustment_blend_clip_and_text_effects_render(self):
        assets={a['id']:a for a in self.app.state.store.assets(self.pid)};project=self.app.state.store.project_dir(self.pid)
        doc=Document.model_validate({'width':120,'height':80,'background':'#ffffff','layers':[
          {'id':'base','kind':'image','asset':self.aid,'width':120,'height':80,'name':'原图','brightness':1.2,'contrast':1.1,'saturation':.8,'blend_mode':'multiply'},
          {'id':'text','kind':'text','text':'很长的自动换行文字','x':4,'y':4,'width':60,'height':70,'size':18,'color':'#ffffff','stroke_color':'#000000','stroke_width':2,'shadow_blur':2,'shadow_x':2,'shadow_y':2,'text_align':'center','clip_to_below':True},
          {'id':'adjust','kind':'adjustment','name':'整体调整','brightness':1.1,'temperature':.2,'opacity':.6}]})
        out=self.root/'adjusted';out.mkdir();export_document(doc,assets,project,out,'png')
        with Image.open(out/'image.png') as im:self.assertEqual(im.size,(120,80));self.assertNotEqual(im.getpixel((0,0)),(56,89,57,255))
        transparent=Document.model_validate({'width':120,'height':80,'background':'transparent','layers':[{'id':'only','kind':'image','asset':self.aid,'width':120,'height':80,'name':'首层','blend_mode':'multiply'}]})
        out2=self.root/'transparent-blend';out2.mkdir();export_document(transparent,assets,project,out2,'png')
        with Image.open(out2/'image.png') as im:self.assertEqual(im.getpixel((10,10)),(56,89,57,255))

    def test_sam3_zim_pipeline_publishes_foreground_mask_metadata_and_previews(self):
        store=self.app.state.store;project=store.project_dir(self.pid);assets=store.assets(self.pid);out=project/'edit/sam3-mock'
        def fake(action,source,target,parameters,work,cancel):
            Image.new('L',(120,80),255).save(target);Path(parameters['metadata']).write_text(json.dumps({'segmenter':'sam3','matting':'zim'}))
            Image.new('RGB',(120,80),'red').save(Path(work)/'sam3-preview.jpg');Image.new('RGB',(120,80),'blue').save(Path(work)/'zim-preview.jpg')
        with patch('webapp.image_tasks.model_image',side_effect=fake):files=execute_image({'action':'segment_sam3','asset':self.aid,'prompt':'cup','parameters':{'refine':True}},assets,project,out)
        self.assertEqual(set(files),{'foreground.png','mask.png','sam3-zim.json','sam3-preview.jpg','zim-preview.jpg'})
        self.assertEqual(json.loads((out/'sam3-zim.json').read_text())['matting'],'zim')

if __name__=='__main__':unittest.main()

class ProviderContractTests(unittest.TestCase):
    def test_remote_media_type_and_cancel_on_abort(self):
        import threading
        from unittest.mock import Mock
        from webapp.image_providers import remote_images
        with tempfile.TemporaryDirectory(dir=isolated_tmp_root()) as folder:
            root=Path(folder);src=root/'source.png';Image.new('RGB',(256,256)).save(src)
            session=Mock();session.__enter__=Mock(return_value=session);session.__exit__=Mock(return_value=False)
            session.post.return_value.status_code=202;session.post.return_value.json.return_value={'id':'image-editor_test'}
            status=Mock();status.json.return_value={'status':'succeeded','stage':'done','progress':100,'artifacts':[{'id':'artifact-1','name':'edited-image.png','media_type':'image/png','kind':'image','url':'http://untrusted.example/private'}]}
            download=Mock();download.__enter__=Mock(return_value=download);download.__exit__=Mock(return_value=False);download.iter_content.return_value=[src.read_bytes()]
            session.get.side_effect=[status,download]
            with patch('webapp.image_providers.requests.Session',return_value=session):
                results=remote_images('edit',src,root/'out',{'prompt':'x'})
            self.assertEqual(results[0]['type'],'image/png')
            self.assertTrue(results[0]['path'].is_file())
            self.assertEqual(session.get.call_args.args[0],'http://127.0.0.1:8911/api/jobs/image-editor_test/artifacts/artifact-1')
            event=threading.Event();session.reset_mock();session.post.return_value.json.return_value={'id':'image-editor_cancel'}
            def cancel_after_submit(*args,**kwargs):event.set();return session.post.return_value
            session.post.side_effect=cancel_after_submit
            with patch('webapp.image_providers.requests.Session',return_value=session),self.assertRaisesRegex(RuntimeError,'取消'):
                remote_images('edit',src,root/'cancelled',{'prompt':'x'},cancel=event)
            self.assertTrue(session.post.call_args.args[0].endswith('/image-editor_cancel/cancel'))

class ImagePixelTests(unittest.TestCase):
    def test_pixelate_changes_region_and_does_not_touch_outside(self):
        import sys
        sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'helpers'))
        from image_edit import validate,render_plan
        with tempfile.TemporaryDirectory(dir=isolated_tmp_root()) as directory:
            root=Path(directory);(root/'edit').mkdir()
            data=np.random.default_rng(42).integers(0,256,(64,64,3),dtype=np.uint8);Image.fromarray(data).save(root/'source.png')
            p=validate({'version':1,'sources':{'main':'source.png'},'operations':[{'type':'pixelate','x':10,'y':10,'width':20,'height':20,'block':8}]},root)
            render_plan(p,Path('edit/result.png'));out=np.asarray(Image.open(root/'edit/result.png').convert('RGB'))
            self.assertTrue(np.array_equal(out[:10],data[:10]));self.assertFalse(np.array_equal(out[10:30,10:30],data[10:30,10:30]))
