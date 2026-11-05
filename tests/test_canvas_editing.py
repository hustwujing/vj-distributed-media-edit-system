import base64
import copy
import io
import json
import os
import pwd
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import Mock

from fastapi import HTTPException
from PIL import Image

import test_image_workbench as workbench_tests
from webapp import agent_store
from webapp.image_canvas import bind_context,agent_patch
from webapp.agent_gateway import Gateway


class CanvasEditingTests(unittest.TestCase):
    setUp=workbench_tests.ImageWorkbenchTests.setUp
    tearDown=workbench_tests.ImageWorkbenchTests.tearDown

    @property
    def base(self):return f'/api/projects/{self.pid}'
    @property
    def store(self):return self.app.state.store
    def save(self,doc=None):
        r=self.a.put(self.base+'/image-document',json=doc or self.doc)
        self.assertEqual(r.status_code,200,r.text)
        return self.a.get(self.base+'/image-document').json()['document']
    def lock_layer(self,layer='original',revision=1):
        r=self.a.post(self.base+'/image-layer-lock',json={'revision':revision,'layer_id':layer,'locked':True})
        self.assertEqual(r.status_code,200,r.text);return r.json()['document']
    def running(self,mode='execute',layer='box',revision=1):
        ctx=bind_context(self.store,self.pid,{'revision':revision,'layer_id':layer})
        turn=agent_store.add_turn(self.store,self.pid,mode,'改成蓝色',canvas_context=ctx)
        self.store.execute("UPDATE agent_turns SET status='running' WHERE id=?",(turn,));return turn
    def assertHttp(self,code,fn):
        with self.assertRaises(HTTPException) as error:fn()
        self.assertEqual(error.exception.status_code,code)

    def test_locked_content_cannot_be_changed_deleted_unlocked_or_reordered(self):
        self.save();doc=self.lock_layer()
        for field,value in [('x',99),('visible',False),('locked',False),('asset','foreign'),('name','改名')]:
            changed=copy.deepcopy(doc);changed['layers'][0][field]=value
            self.assertIn(self.a.put(self.base+'/image-document',json=changed).status_code,[409,422])
        deleted=copy.deepcopy(doc);deleted['layers'].pop(0)
        reordered=copy.deepcopy(doc);reordered['layers'].reverse()
        for changed in (deleted,reordered):self.assertEqual(self.a.put(self.base+'/image-document',json=changed).status_code,409)
        self.assertEqual(self.b.post(self.base+'/image-layer-lock',json={'revision':2,'layer_id':'original','locked':False}).status_code,404)
        self.assertEqual(self.a.post(self.base+'/image-layer-lock',json={'revision':1,'layer_id':'original','locked':False}).status_code,409)
        unlocked=self.a.post(self.base+'/image-layer-lock',json={'revision':2,'layer_id':'original','locked':False}).json()['document']
        self.assertFalse(unlocked['layers'][0]['locked']);self.assertEqual(unlocked['revision'],3)
        unlocked['layers'][0]['x']=15;self.save(unlocked)
        self.assertEqual(self.a.get(self.base+'/image-document/2').json()['layers'][0]['x'],0)

    def test_agent_bound_layer_plan_check_and_atomic_apply(self):
        self.save();before=self.lock_layer();turn=self.running('plan',revision=2)
        checked=agent_patch(self.store,self.pid,turn,{'patch':{'color':'#0000ff'}})
        self.assertFalse(checked['applied']);self.assertEqual(checked['after']['revision'],2)
        self.assertHttp(403,lambda:agent_patch(self.store,self.pid,turn,{'patch':{'color':'#0000ff'},'apply':True}))
        agent_store.finish(self.store,turn,'succeeded');turn=self.running(revision=2)
        for invalid in ({'locked':False},{'id':'original'},{'kind':'image'},{'width':float('nan')},{'x':999999}):
            self.assertHttp(422,lambda:agent_patch(self.store,self.pid,turn,{'patch':invalid,'apply':True}))
        result=agent_patch(self.store,self.pid,turn,{'patch':{'color':'#0000ff','x':33},'apply':True})
        self.assertEqual(result['after']['layers'][0],before['layers'][0]);self.assertEqual(result['after']['layers'][1]['x'],33)
        self.assertEqual(result['after']['revision'],3)
        self.assertHttp(409,lambda:agent_patch(self.store,self.pid,turn,{'patch':{'x':44},'apply':True}))
        self.assertEqual(agent_store.public(agent_store.turn(self.store,turn))['canvas_result']['after'],3)
        self.assertEqual(self.a.get(self.base+'/image-document').json()['versions'][0]['change']['turn'],turn)

    def test_stale_or_cancelled_agent_cannot_apply(self):
        doc=self.save();turn=self.running();doc['width']=130;self.save(doc)
        self.assertHttp(409,lambda:agent_patch(self.store,self.pid,turn,{'patch':{'x':22},'apply':True}))
        self.store.execute("UPDATE agent_turns SET status='cancelling' WHERE id=?",(turn,))
        self.assertHttp(409,lambda:agent_patch(self.store,self.pid,turn,{'patch':{'x':22},'apply':True}))
        self.assertEqual(self.a.get(self.base+'/image-document').json()['document']['layers'][1]['x'],10)

    def test_pixel_edits_cannot_be_reported_as_unused_layer_parameters(self):
        self.save();turn=self.running(layer='original')
        self.assertHttp(422,lambda:agent_patch(self.store,self.pid,turn,{'patch':{'color':'#0000ff'},'apply':True}))
        self.assertHttp(422,lambda:agent_patch(self.store,self.pid,turn,{'patch':{'text':'假标题'},'apply':True}))
        self.assertEqual(self.a.get(self.base+'/image-document').json()['document']['revision'],1)

    def test_message_context_inherits_plan_rejects_override_and_stale_version(self):
        self.save();(self.root/'agent-health.json').write_text(json.dumps({'ready':True,'updated':time.time()}))
        endpoint=self.base+'/agent/messages';context={'revision':1,'layer_id':'box'}
        for bad in ({'revision':1,'layer_id':'unknown'},{'revision':2,'layer_id':'box'},{'revision':1,'layer_id':'box','project':'foreign'}):
            self.assertIn(self.a.post(endpoint,json={'message':'修改','canvas_context':bad}).status_code,[404,409,422])
        plan=self.a.post(endpoint,json={'message':'改颜色','canvas_context':context}).json()
        self.assertEqual(plan['canvas_context']['layer']['name'],'色块')
        from tests.readiness_support import ready_plan
        ready_plan(self.store,plan['id'])
        changed={**context,'layer_id':'original'}
        self.assertEqual(self.a.post(endpoint,json={'message':'执行','mode':'execute','plan_id':plan['id'],'canvas_context':changed}).status_code,409)
        executed=self.a.post(endpoint,json={'message':'执行','mode':'execute','plan_id':plan['id']})
        self.assertEqual(executed.status_code,202,executed.text)
        self.assertEqual(executed.json()['canvas_context'],plan['canvas_context'])
        agent_store.finish(self.store,executed.json()['id'],'succeeded')
        plan=self.a.post(endpoint,json={'message':'继续','canvas_context':context}).json();agent_store.finish(self.store,plan['id'],'succeeded')
        self.lock_layer()
        self.assertEqual(self.a.post(endpoint,json={'message':'执行','mode':'execute','plan_id':plan['id']}).status_code,409)

    def test_candidate_application_preserves_geometry_checks_owner_lock_and_revision(self):
        self.save();buf=io.BytesIO();Image.new('RGB',(24,16),'blue').save(buf,format='PNG')
        aid=self.a.post(self.base+'/assets',files={'file':('candidate.png',buf.getvalue(),'image/png')}).json()['id']
        endpoint=self.base+'/image-apply';body={'revision':1,'layer_id':'original','asset':aid}
        self.assertEqual(self.b.post(endpoint,json=body).status_code,404)
        self.assertEqual(self.a.post(endpoint,json={**body,'asset':'foreign'}).status_code,422)
        applied=self.a.post(endpoint,json=body);self.assertEqual(applied.status_code,200,applied.text)
        layer=applied.json()['document']['layers'][0];self.assertEqual((layer['width'],layer['height']),(120,80));self.assertEqual(layer['asset'],aid)
        self.assertEqual(self.a.post(endpoint,json=body).status_code,409)
        self.lock_layer(revision=2)
        self.assertEqual(self.a.post(endpoint,json={**body,'revision':3}).status_code,409)
        self.assertEqual(self.a.get(self.base+'/image-document/1').json()['layers'][0]['asset'],self.aid)

    def test_two_concurrent_applications_do_not_overwrite(self):
        self.save();turn=self.running()
        def apply(x):
            try:return agent_patch(self.store,self.pid,turn,{'patch':{'x':x},'apply':True})['after']['revision']
            except HTTPException as exc:return exc.status_code
        with ThreadPoolExecutor(max_workers=2) as pool:results=list(pool.map(apply,[20,30]))
        self.assertEqual(sorted(results),[2,409]);self.assertEqual(len(self.a.get(self.base+'/image-document').json()['versions']),2)

    def test_gateway_applies_python_image_bytes_without_paths_or_root_decoding(self):
        self.save();turn=self.running(layer='original')
        g=object.__new__(Gateway);g.mode='execute';g.project_dir=self.store.project_dir(self.pid);g.notify=Mock()
        account=pwd.getpwnam('video-use');g.uid=account.pw_uid;g.gid=account.pw_gid
        g.canvas_callback=lambda body,asset:agent_patch(self.store,self.pid,turn,body,asset)
        buf=io.BytesIO();Image.new('RGB',(40,30),'blue').save(buf,format='PNG')
        response=g.canvas_result({'apply':True,'patch':{},'image':base64.b64encode(buf.getvalue()).decode()})
        self.assertEqual(response.status,200)
        result=json.loads(b''.join(response.chunks));self.assertTrue(result['applied']);self.assertEqual(result['after']['revision'],2)
        layer=result['after']['layers'][0];asset=next(a for a in self.store.assets(self.pid) if a['id']==layer['asset'])
        self.assertEqual(asset['metadata']['width'],40)
        with Image.open(g.project_dir/'sources'/asset['filename']) as im:self.assertEqual(im.getpixel((10,10)),(0,0,255,255))
        count=len(list((g.project_dir/'sources').iterdir()))
        response=g.canvas_result({'apply':True,'patch':{},'image':base64.b64encode(buf.getvalue()).decode()})
        self.assertEqual(response.status,409);self.assertEqual(len(list((g.project_dir/'sources').iterdir())),count)


if __name__=='__main__':unittest.main()
