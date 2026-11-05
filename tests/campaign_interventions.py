"""Repeatable live R01–R16 fixtures. Only uniquely named QA projects are mutated.

Planning records are seeded by explicit user authorization. All step outputs,
reuse receipts, acceptance and final delivery come from deployed executors.
Never imports mocks or writes successful step/final-execution states.
"""
from __future__ import annotations
import argparse
from concurrent.futures import ThreadPoolExecutor
import copy
import hashlib
import io
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import time
import uuid

REPO=Path(__file__).resolve().parents[1]
DOC=REPO/'docs/real-case-campaign'
ROOT=REPO/'.cluster/campaigns/20261031-real60'
DRIVER_SOURCE=Path(__file__).read_bytes()
DRIVER_SHA256=hashlib.sha256(DRIVER_SOURCE).hexdigest()
EXPORT_SCRIPT=r'''import json,os,sys,time,subprocess
from pathlib import Path
from PIL import Image,ImageOps,ImageDraw
inputs=json.loads(os.environ['VIDEO_USE_STEP_INPUTS'])
output=Path(os.environ['VIDEO_USE_STEP_OUTPUT'])
c=json.loads(sys.argv[1]);time.sleep(c.get('delay',0))
a=Image.open(inputs['a'][0]).convert('RGB');b=Image.open(inputs['b'][0]).convert('RGB')
w,h=c['width'],c['height']
images=[a,b]
im=Image.new('RGB',(w,h))
for i,image in enumerate(images):im.paste(ImageOps.fit(image,(w//2,h)),(i*(w//2),0))
if c['variant']=='feedback':ImageDraw.Draw(im).rectangle((0,0,w-1,h-1),outline='#FF0000',width=4)
for name in c['outputs']:
 if name.endswith('.mp4'):
  color='yellow' if c['variant']=='caption' else 'white'
  vf=f"scale={w}:{h}:force_original_aspect_ratio=increase,crop={w}:{h},drawtext=fontfile=/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf:text=QA:fontsize=14:fontcolor={color}:x=4:y=4"
  subprocess.run(['ffmpeg','-nostdin','-v','error','-i',inputs['clip'][0],'-t',str(c.get('duration',4)),
    '-vf',vf,'-c:v','libx264','-preset','veryfast','-crf','18','-pix_fmt','yuv420p','-c:a','aac','-threads','2','-y',str(output/name)],check=True)
 elif name=='cover.png':ImageOps.fit(b if c['variant']=='cover' else a,(128,64)).save(output/name)
 elif name=='thumbnail.png':im.resize((32,16),Image.Resampling.LANCZOS).save(output/name)
 elif name.endswith('.jpg'):im.save(output/name,quality=95)
 else:im.save(output/name)
if c.get('completion_gate'):
 print(json.dumps({'type':'step_progress','text':'QA_EXPORT_OUTPUT_READY'}),flush=True)
 time.sleep(5)
'''


def save(path,value):
    path=Path(path);path.parent.mkdir(parents=True,exist_ok=True)
    tmp=path.with_suffix(path.suffix+'.new');tmp.write_text(json.dumps(value,ensure_ascii=False,indent=2)+'\n');tmp.chmod(0o600);tmp.replace(path)


def specification():
    manifest=json.loads((DOC/'cases.json').read_text())
    spec=json.loads((DOC/'intervention-design-fingerprint.json').read_text())
    cases=[c for c in manifest['cases'] if c['id'] in spec['ids']]
    frozen=[{k:c[k] for k in spec['immutable_fields']} for c in cases]
    if hashlib.sha256(json.dumps(frozen,ensure_ascii=False,sort_keys=True).encode()).hexdigest()!=spec['sha256']:
        raise ValueError('Intervention definitions changed; review and version the case collection')
    return cases


def configure():
    sys.path.insert(0,str(REPO))
    from deploy import dynamic_cluster as cluster
    release=json.loads((cluster.ROOT/'release.json').read_text())
    source=Path(release['source']);sys.path.insert(0,str(source))
    os.environ.update(cluster.environment())
    cluster.validate_isolation(os.environ)
    os.environ['VIDEO_USE_RUNTIME_BUILD']=str(source/'runtime/build')
    from webapp.store import Store
    from webapp.operations import get
    if not get('image.local_adjust').reuse_safe:raise RuntimeError('Deploy reuse support before running these cases')
    store=Store(cluster.data('node','a'),initialize_schema=False)
    if not store.query("SELECT name FROM sqlite_master WHERE name='workflow_reuse_receipts'"):
        raise RuntimeError('Deployed schema does not support reuse')
    return cluster,release


class Case:
    def __init__(self,spec,args,cluster,release):
        import requests
        from webapp.store import Store
        self.spec,self.args,self.cluster,self.release=spec,args,cluster,release
        self.store=Store(cluster.data('node','a'),initialize_schema=False)
        self.session=requests.Session();self.session.trust_env=False
        self.run_id=time.strftime('%Y%m%dT%H%M%S')+'-'+uuid.uuid4().hex[:8]
        folder=ROOT/'runs'/spec['id'];folder.mkdir(parents=True,exist_ok=True)
        attempt=max([int(p.name.split('-')[1]) for p in folder.glob('attempt-*') if p.is_dir()]+[0])+1
        self.directory=folder/('attempt-'+str(attempt).zfill(3));self.directory.mkdir()
        self.state={'case':spec['id'],'title':spec['title'],'status':'running','run_id':self.run_id,
            'started_at':time.time(),'release':release,'entry_mode':spec['entry_mode'],'fixture':spec['fixture'],
            'events':[],'versions':[],'artifacts':[],'planning_source':'database_fixture','expected_external_generation_calls':0}
        self.state['driver_sha256']=DRIVER_SHA256
        (self.directory/'driver-snapshot.py').write_bytes(DRIVER_SOURCE)
        save(self.directory/'case-definition.json',spec)
        self.project=None;self.assets={};self.persist()

    def persist(self):save(self.directory/'state.json',self.state)

    def event(self,action,**data):
        self.state['events'].append({'action':action,'at':time.time(),**data});self.persist()
        print(json.dumps({'case':self.spec['id'],'action':action,**{k:v for k,v in data.items() if k in ('task','status','version','error')}},ensure_ascii=False),flush=True)

    def api(self,method,path,*,expected=200,**kwargs):
        response=self.session.request(method,self.args.base+path,timeout=45,**kwargs)
        self.state['events'].append({'action':'http','method':method,'path':path,'status':response.status_code,
            'request_id':response.headers.get('x-request-id'),'at':time.time()})
        if response.status_code!=expected:raise RuntimeError(f'{method} {path}: {response.status_code} {response.text[:500]}')
        return response

    def setup(self):
        self.project=self.api('POST','/api/projects',expected=201,json={'name':'REUSE-QA-'+self.run_id+'-'+self.spec['id']}).json()['id']
        self.state['project']=self.project;self.persist()
        save(self.directory/'private-session.json',self.session.cookies.get_dict())
        for name in self.spec['assets']:
            path=ROOT/'fixtures'/name
            with path.open('rb') as stream:
                sha=hashlib.file_digest(stream,'sha256').hexdigest();stream.seek(0)
                value=self.api('POST',f'/api/projects/{self.project}/assets',expected=201,files={'file':(name,stream)}).json()
            self.assets[name]=value['id']
            self.state.setdefault('fixture_input_hashes',{})[name]=sha
        path=self.store.project_dir(self.project)/'agent/workspace/edit/reuse_campaign.py'
        path.parent.mkdir(parents=True,exist_ok=True);path.write_text(EXPORT_SCRIPT)
        self.store.artifacts.save_file(self.project,'agent/workspace/edit/reuse_campaign.py')
        self.event('fixture_assets_and_script',assets=self.assets,script_sha256=hashlib.sha256(EXPORT_SCRIPT.encode()).hexdigest())

    def roots(self):
        result=[]
        for sid,name in [('prepare_a','portrait.png'),('prepare_b','rocket.png')]:
            result.append({'id':sid,'name':sid,'kind':'operation','operation':'image.local_adjust',
                'inputs':{'source':{'asset':self.assets[name]}},'parameters':{'parameters':{'brightness':1.1}},
                'goal':'将指定图片亮度调整为1.1，作为后续导出素材。','on_failure':'pause',
                'acceptance':[{'kind':'image'}],'basic_reason':'固定的本地亮度调整，中间图片完整性由程序检查。'})
        if 'clip_a.mp4' in self.assets:
            result.append({'id':'clip','name':'clip','kind':'operation','operation':'media.edit',
                'inputs':{'source':{'asset':self.assets['clip_a.mp4']}},
                'parameters':{'edit_plan':{'clips':[{'asset':'source','start':0,'end':4}],
                    'output':{'width':128,'height':64,'fps':24}}},
                'goal':'准备原视频前4秒128×64片段，供后续包装复用。','on_failure':'pause',
                'acceptance':[{'kind':'media','file':'final.mp4','width':128,'height':64,'min_duration':3.9,'max_duration':4.1,'decode':True},
                              {'kind':'file','file':'*'}]})
        return result

    def export(self,version,*,gate=False):
        variant=self.spec['fixture']['variant'] if version>1 else 'baseline'
        kind=self.spec['expect']['kind']
        if variant=='baseline' and self.spec['fixture']['variant']=='new_goal':kind='image'
        width,height=(self.spec['expect']['width'],self.spec['expect']['height']) if version>1 else (128,64)
        outputs=['final.mp4','cover.png'] if kind=='video' else ['final.png']
        if variant=='rename':outputs=['renamed.png']
        if variant=='resize_format':outputs=['final.webp']
        if variant=='add_format':outputs=['final.png','final.jpg']
        if variant=='thumbnail':outputs=['final.png','thumbnail.png']
        if variant=='chain' and version==2:outputs=['final.jpg']
        if variant=='chain' and version==3:outputs=['final.png','thumbnail.png']
        stage=self.spec['fixture']['stage'];delay=8 if version==1 and stage in ('partial_execution','export_running','running_to_paused','completion_race') else 0
        if version>1 and variant=='refresh':delay=5
        config={'variant':variant,'width':width,'height':height,'kind':kind,'outputs':outputs,'delay':delay,
                'duration':self.spec['expect'].get('duration',4) if version>1 else 4,
                'completion_gate':version==1 and stage=='completion_race'}
        inputs={'a':{'step':'prepare_a','role':'primary_image'},'b':{'step':'prepare_b','role':'primary_image'}}
        if variant=='reorder':inputs['a'],inputs['b']=inputs['b'],inputs['a']
        if 'clip_a.mp4' in self.assets:inputs['clip']={'step':'clip','role':'video'}
        checks=[{'kind':'file','file':'*'}]
        if kind=='video':checks.append({'kind':'media','file':'final.mp4','width':width,'height':height,
            'min_duration':config['duration']-.15,'max_duration':config['duration']+.15,'decode':True})
        else:checks.append({'kind':'image','file':outputs[0],'width':width,'height':height})
        if gate:checks.append({'kind':'review','file':outputs[0],'criterion':'请用户检查当前导出，确认是否继续；这是本测试明确要求的人工验收点。'})
        spec={'id':'export','name':'导出第'+str(version)+'版','kind':'script','inputs':inputs,'depends_on':[r['step'] for r in inputs.values()],
            'argv':['python','edit/reuse_campaign.py',json.dumps(config,separators=(',',':'))],
            'outputs':outputs,'output_roles':{'video' if kind=='video' else 'primary_image':outputs[0]},
            'goal':f'交付第{version}版：'+json.dumps(config,ensure_ascii=False)+'。主文件为'+outputs[0]+'，中间图不作为主结果。',
            'acceptance':checks,'on_failure':'pause','timeout':180}
        return spec,config

    def plan(self,version,roots,*,gate=False,parent=None,execute=True,draft_baseline=False):
        from webapp import agent_store,workflows,execution_intent,plan_readiness
        export,config=self.export(1 if draft_baseline else version,gate=gate);steps=[*roots,export]
        if version==1 and self.spec['fixture']['stage']=='partial_execution':
            # A real intermediate script gives a deterministic intervention
            # boundary after preprocessing and before export is ever claimed.
            staging={'id':'staging','name':'汇总交付元数据','kind':'script',
                'inputs':copy.deepcopy(export['inputs']),'depends_on':list(export['depends_on']),
                'argv':['python','-c',"import os,time,json;from pathlib import Path;time.sleep(8);Path(os.environ['VIDEO_USE_STEP_OUTPUT'],'ready.json').write_text(json.dumps({'ready':True}))"],
                'outputs':['ready.json'],'output_roles':{'report':'ready.json'},
                'goal':'汇总已完成预处理的交付元数据；这是可重复干预窗口。','on_failure':'pause',
                'acceptance':[{'kind':'json','required_keys':['ready']}],'timeout':60}
            export['depends_on'].append('staging');steps=[*roots,staging,export]
        goal='可重复回归测试，规划状态由夹具构造；请按确认目标实际执行并验收。'+export['goal']
        with self.store.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            plan=agent_store.add_turn(self.store,self.project,'plan',goal,parent_task_id=parent)
            db.execute("UPDATE agent_turns SET status='running' WHERE id=?",(plan,))
            execution_intent.declare(self.store,plan,{'route':'workflow','operations':[s.get('operation','script') for s in steps],
                'goal':goal,'estimated_seconds':180})
            agent_store.finish(self.store,plan,'succeeded',goal)
            db.execute("UPDATE plan_readiness SET state='ready',reason='QA fixture: explicit reviewed proposal' WHERE run_id=?",(plan,))
        item={'version':version,'plan_id':plan,'definition':{'key':self.spec['id']+'-v'+str(version),'name':self.spec['title'],'steps':steps},'config':config}
        self.state['versions'].append(item);self.event('fixture_plan_created',version=version,plan_id=plan)
        if execute:self.execute(item)
        return item

    def execute(self,item):
        from webapp import agent_store,workflows,plan_readiness
        with self.store.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            task=agent_store.add_turn(self.store,self.project,'execute','确认执行：'+item['definition']['steps'][-1]['goal'],plan_id=item['plan_id'])
            plan_readiness.consume(self.store,self.project,item['plan_id'],task)
            db.execute("UPDATE agent_turns SET status='running' WHERE id=?",(task,))
            workflows.submit(self.store,task,item['definition']);workflows.state(self.store,task,'ready')
        item['task']=task;self.state['execute_id']=task;self.state['execute_release']=self.release
        self.event('confirmed_fixture_dispatched',task=task,version=item['version'])

    def task(self,identifier):
        turns=self.api('GET',f'/api/projects/{self.project}/agent').json()['turns']
        return next(t for t in turns if t['id']==identifier)

    def wait(self,item,predicate,*,allow_pause=False):
        deadline=time.monotonic()+self.args.timeout;previous=None
        while time.monotonic()<deadline:
            current=json.loads((self.cluster.ROOT/'release.json').read_text())
            if current['sha256']!=self.release['sha256']:raise RuntimeError('Release changed during case; retain evidence and rerun')
            row=self.task(item['task']);w=row.get('workflow') or {}
            signature=(row['status'],tuple((s['id'],s['status'],s['revision']) for s in w.get('steps',[])))
            if signature!=previous:
                previous=signature;save(self.directory/(str(item['version'])+'-latest.json'),row)
                self.event('state',task=item['task'],version=item['version'],status=row['status'],steps=signature[1])
            if predicate(row):return row
            if row['status'] in ('failed','cancelled') or row['status']=='paused' and not allow_pause:
                raise RuntimeError('Unexpected terminal state: '+row['status']+' '+str(row.get('error')))
            time.sleep(.25)
        raise TimeoutError('Observation timeout; case will cancel its own tasks during cleanup')

    def cancel(self,item):
        self.api('POST',f'/api/projects/{self.project}/agent/{item["task"]}/cancel',json={})
        row=self.wait(item,lambda t:t['status']=='cancelled',allow_pause=True)
        self.event('user_cancelled_old_version',task=item['task'])
        return row

    def revised_roots(self,item,version=2):
        from webapp import workflows,workflow_reuse
        w=workflows.get(self.store,item['task']);by={s['id']:s for s in w['steps']};roots=[]
        for original in self.roots():
            sid=original['id'];source=by[sid]
            if source['status']!='succeeded':raise AssertionError('Source step did not complete: '+sid)
            new=copy.deepcopy(source['spec']);new['reuse_from']=None
            if sid in self.spec['must_recompute'] and version==2:
                if self.spec['fixture']['variant']=='change_brightness':new['parameters']['parameters']['brightness']=1.35
                else:new['inputs']['source']['asset']=self.assets['rocket.png']
            else:
                ref={'workflow':w['id'],'step':sid,'revision':source['revision']}
                receipt=workflow_reuse.source_receipt(self.store,self.project,ref)
                new['reuse_from']=ref
                save(self.directory/(f'source-v{version}-{sid}.json'),receipt)
            roots.append(new)
        return roots

    def inspect_reuse(self,item):
        from webapp import workflows,workflow_reuse
        w=workflows.get(self.store,item['task']);proof=[]
        for s in w['steps']:
            if s['id']=='export':continue
            assert s['status']=='succeeded',s['id']
            requested=s['spec'].get('reuse_from');actual=s['bindings'].get('_reuse')
            if requested:
                assert actual and all(actual[k]==requested[k] for k in ('workflow','step','revision'))
                source=workflow_reuse.source_receipt(self.store,self.project,requested)
                expected=sorted(r['sha256'] for r in source['manifest'].values())
                actual_hashes=sorted(f['sha256'] for f in s['quality']['files'])
                assert actual_hashes==expected,(s['id'],expected,actual_hashes)
                assert all(f.startswith(f'edit/workflows/{w["id"]}/{s["id"]}/') for f in s['result'])
                calls=self.store.query("SELECT operation FROM usage_calls WHERE task_id=? AND step_id=? AND category='tool'",(item['task'],s['id']))
                proof.append({'step':s['id'],'reused':True,'source':actual,'hashes':actual_hashes,'tool_calls':len(calls)})
            else:
                assert not actual;proof.append({'step':s['id'],'reused':False,'attempts':s['attempts'],'hashes':[f['sha256'] for f in s['quality']['files']]})
        if item['version']==2:
            assert set(self.spec['must_reuse']).issubset({p['step'] for p in proof if p['reused']})
            assert set(self.spec['must_recompute']).issubset({p['step'] for p in proof if not p['reused']})
        self.state.setdefault('reuse_checks',[]).append({'version':item['version'],'nodes':proof});self.persist()
        budget=self.store.query("SELECT used FROM execution_budgets WHERE run_id=? AND category='local_image'",(item['task'],))
        expected_calls=sum(s['spec']['operation']=='image.local_adjust' and not s['spec'].get('reuse_from') for s in w['steps'] if s['spec']['kind']=='operation')
        assert (budget[0]['used'] if budget else 0)==expected_calls,'Reused image operations were executed again'

    def download_and_check(self,item,row):
        from PIL import Image,ImageOps,ImageDraw
        from webapp import workflows
        w=workflows.get(self.store,item['task']);s=next(s for s in w['steps'] if s['id']=='export');config=item['config']
        folder=self.directory/('output-v'+str(item['version']));folder.mkdir(exist_ok=True)
        artifacts=[];by_name={}
        for a in row.get('artifacts',[]):
            if not any(a['name']==str(Path(f).relative_to('edit')) for f in s['result']):continue
            from urllib.parse import urljoin,urlsplit
            url=urljoin(self.args.base+'/',a['url'])
            if urlsplit(url).netloc!=urlsplit(self.args.base).netloc:raise ValueError('Unexpected output origin')
            r=self.session.get(url,timeout=60);r.raise_for_status();target=folder/Path(a['name']).name;target.write_bytes(r.content)
            record={**a,'local_file':str(target),'bytes':len(r.content),'sha256':hashlib.sha256(r.content).hexdigest()}
            artifacts.append(record);by_name[target.name]=target
        assert set(config['outputs']).issubset(by_name),list(by_name)
        primary=next((a for a in row.get('artifacts',[]) if a.get('is_primary')),None)
        assert primary and Path(primary['name']).name==config['outputs'][0],'Primary delivery differs from the confirmed plan'
        assert any(a['name']==primary['name'] for a in artifacts),'Primary delivery is not the final export'
        # Independent pixel reference from the actual accepted upstream files.
        images=[]
        for sid in ('prepare_a','prepare_b'):
            source=next(x for x in w['steps'] if x['id']==sid)
            ref=source['quality']['output_roles']['primary_image']
            checkpoint=self.store.query('SELECT manifest FROM execution_step_checkpoints WHERE workflow=? AND step=? AND revision=?',(w['id'],sid,source['revision']))[0]
            record=json.loads(checkpoint['manifest'])[ref]
            path=self.store.artifacts.materialize(self.project,record['artifact'])
            images.append(Image.open(path).convert('RGB'))
        if config['variant']=='reorder':images.reverse()
        expected=Image.new('RGB',(config['width'],config['height']))
        for index,image in enumerate(images):expected.paste(ImageOps.fit(image,(config['width']//2,config['height'])),(index*(config['width']//2),0))
        if config['variant']=='feedback':ImageDraw.Draw(expected).rectangle((0,0,expected.width-1,expected.height-1),outline='#FF0000',width=4)
        checks=[]
        for name,path in by_name.items():
            if path.suffix=='.mp4':
                data=json.loads(subprocess.check_output(['ffprobe','-v','error','-show_streams','-show_format','-of','json',str(path)],text=True,timeout=30))
                video=next(st for st in data['streams'] if st['codec_type']=='video')
                assert (video['width'],video['height'])==(config['width'],config['height'])
                assert abs(float(data['format']['duration'])-config['duration'])<.2
                subprocess.run(['ffmpeg','-v','error','-i',str(path),'-f','null','-'],check=True,capture_output=True,timeout=45)
                upstream=next(x for x in w['steps'] if x['id']=='clip')
                upstream_path=upstream['quality']['output_roles']['video']
                checkpoint=self.store.query('SELECT manifest FROM execution_step_checkpoints WHERE workflow=? AND step=? AND revision=?',(w['id'],'clip',upstream['revision']))[0]
                stored=json.loads(checkpoint['manifest'])[upstream_path]
                source_file=self.store.artifacts.materialize(self.project,stored['artifact'])
                def frame(file):
                    raw=subprocess.check_output(['ffmpeg','-v','error','-i',str(file),'-ss','0.5','-frames:v','1','-threads','1','-f','image2pipe','-c:v','png','-'],timeout=30)
                    return Image.open(io.BytesIO(raw)).convert('RGB')
                import numpy as np
                actual_frame=np.asarray(frame(path)).astype(float)
                expected_frame=np.asarray(ImageOps.fit(frame(source_file),(config['width'],config['height']))).astype(float)
                frame_error=float(abs(actual_frame[30:]-expected_frame[30:]).mean())
                assert frame_error<18,('Unexpected video content',frame_error)
                if config['variant']=='caption':
                    text=actual_frame[2:26,2:34]
                    assert int(((text[:,:,0]>150)&(text[:,:,1]>140)&(text[:,:,2]<110)).sum())>=12,'Yellow caption missing'
                checks.append({'file':name,'decoded':True,'width':video['width'],'height':video['height'],'duration':data['format']['duration'],'source_frame_mae':frame_error})
            else:
                loaded=Image.open(path)
                assert loaded.format=={'.png':'PNG','.jpg':'JPEG','.webp':'WEBP'}[path.suffix],name
                actual=loaded.convert('RGB')
                wanted=(expected.resize((32,16),Image.Resampling.LANCZOS) if name=='thumbnail.png' else
                    ImageOps.fit(images[1] if config['variant']=='cover' else images[0],(128,64)) if name=='cover.png' else expected)
                assert actual.size==wanted.size
                import numpy as np
                error=float(abs(np.asarray(actual).astype(float)-np.asarray(wanted).astype(float)).mean())
                assert error<(12 if path.suffix in ('.jpg','.webp') else .01),(name,error)
                checks.append({'file':name,'pixel_mae':error,'size':list(actual.size),'format':loaded.format})
        self.state['artifacts']=artifacts;self.state['verification']={'passed':True,'checks':checks,'artifacts':artifacts}
        self.state['content_review']={'passed':True,'method':'independent pixels, actual decoded media, fixed reuse provenance',
            'note':'图片按实际上游产物独立比较像素；视频完整解码并核对尺寸时长。'}
        save(self.directory/('v'+str(item['version'])+'-execute.json'),row);save(self.directory/'execute.json',row);self.persist()

    def refresh(self,item):
        auth=self.directory/'private-cookies.json'
        save(auth,[{'name':c.name,'value':c.value,'domain':c.domain,'path':c.path,'httpOnly':True,'secure':False,'sameSite':'Lax'} for c in self.session.cookies])
        try:
            subprocess.run(['node',str(REPO/'tests/campaign_interventions_reload.cjs'),self.args.base,self.project,str(auth),str(self.directory)],check=True,timeout=60,capture_output=True)
            self.event('browser_reloaded_and_reentered',task=item['task'])
        finally:auth.unlink(missing_ok=True)

    def cleanup(self):
        if not self.project:return {'passed':True,'project_created':False}
        pid=self.project
        project=self.store.query('SELECT name FROM projects WHERE id=?',(pid,))
        if project and project[0]['name']!='REUSE-QA-'+self.run_id+'-'+self.spec['id']:raise ValueError('Refusing cleanup outside this unique fixture')
        tasks=[r['id'] for r in self.store.query('SELECT id FROM agent_turns WHERE project=?',(pid,))]
        ledger_tasks=[r['task_id'] for r in self.store.query('SELECT task_id FROM usage_tasks WHERE project_id=?',(pid,))]
        for task in tasks:
            current=self.store.query('SELECT status FROM agent_turns WHERE id=?',(task,))[0]['status']
            if current in ('queued','running','waiting','paused','cancelling'):
                self.api('POST',f'/api/projects/{pid}/agent/{task}/cancel',json={})
        deadline=time.monotonic()+120
        while project:
            response=self.session.delete(self.args.base+'/api/projects/'+pid,timeout=30)
            if response.status_code==200:break
            if response.status_code!=409 or time.monotonic()>deadline:raise RuntimeError('Project cleanup failed: '+response.text[:300])
            time.sleep(.5)
        assert not self.store.query('SELECT path FROM artifact_files WHERE project=?',(pid,))
        assert not self.store.query('SELECT workflow FROM workflow_reuse_receipts WHERE project=?',(pid,))
        objects=self.store.query('SELECT id,object_key FROM artifact_objects WHERE project=?',(pid,))
        for obj in objects:
            self.store.artifacts.backend.delete(obj['object_key'])
            self.store.execute('DELETE FROM artifact_objects WHERE id=? AND project=? AND NOT EXISTS (SELECT 1 FROM artifact_files WHERE artifact=?)',(obj['id'],pid,obj['id']))
        with self.store.connect() as db:
            for task in set(tasks+ledger_tasks):db.execute('DELETE FROM usage_calls WHERE task_id=?',(task,))
            db.execute('DELETE FROM usage_tasks WHERE project_id=?',(pid,))
        for role in ('api','node'):
            for node in ('a','b'):
                path=self.cluster.data(role,node)/'projects'/pid
                if path.exists():shutil.rmtree(path)
        (self.directory/'private-session.json').unlink(missing_ok=True)
        return {'passed':True,'project_removed':pid,'tasks_removed':len(tasks),'objects_removed':len(objects)}

    def run(self):
        from webapp import workflows
        try:
            self.setup();stage=self.spec['fixture']['stage']
            gate=stage in ('planning','awaiting_review','reconnect')
            initial=self.plan(1,self.roots(),gate=gate)
            if stage in ('partial_execution','export_running','running_to_paused','completion_race'):
                boundary='staging' if stage=='partial_execution' else 'export'
                self.wait(initial,lambda t:any(s['id']==boundary and s['status']=='running' for s in (t.get('workflow') or {}).get('steps',[])))
                if stage=='running_to_paused':
                    self.api('POST',f'/api/projects/{self.project}/agent/{initial["task"]}/control',json={'action':'pause'})
                    self.wait(initial,lambda t:t['status']=='paused',allow_pause=True)
                    self.api('POST',f'/api/projects/{self.project}/agent/{initial["task"]}/control',json={'action':'status'})
                    self.event('safe_pause_observed',task=initial['task'])
                if stage=='completion_race':
                    deadline=time.monotonic()+60
                    while not self.store.query("SELECT seq FROM agent_events WHERE run_id=? AND kind='progress' AND payload LIKE ?",(initial['task'],'%QA_EXPORT_OUTPUT_READY%')):
                        if time.monotonic()>deadline:raise TimeoutError('Actual output-completion marker was not observed')
                        time.sleep(.1)
                    self.event('cancel_at_actual_output_boundary',task=initial['task'])
                self.cancel(initial)
                if stage=='partial_execution':
                    old=workflows.get(self.store,initial['task'])
                    assert next(s for s in old['steps'] if s['id']=='export')['attempts']==0
            elif gate:
                self.wait(initial,lambda t:t['status']=='paused' and any(s['status']=='awaiting_review' for s in t['workflow']['steps']),allow_pause=True)
                if stage=='awaiting_review':
                    # Wait for the actual lease to relinquish before feedback.
                    deadline=time.monotonic()+30
                    while self.store.query('SELECT attempt FROM execution_leases WHERE run_id=? AND active=1 AND expires>?',(initial['task'],self.store.now())):
                        if time.monotonic()>deadline:raise TimeoutError('Review lease did not release')
                        time.sleep(.2)
                    self.api('POST',f'/api/projects/{self.project}/agent/{initial["task"]}/quality',json={'step_id':'export','revision':0,'action':'reject','feedback':'请给最终联系表加4像素红色边框，保留已完成预处理。'})
                    self.event('user_quality_rejected',task=initial['task'])
                self.cancel(initial)
            else:
                row=self.wait(initial,lambda t:t['status']=='succeeded');self.download_and_check(initial,row)
            roots=self.revised_roots(initial)
            if stage=='planning':
                old=self.plan(2,roots,parent=initial['task'],execute=False,draft_baseline=True)
                current=self.plan(2,roots,parent=old['plan_id'],execute=False)
                self.event('proposal_changed',before=old['config'],after=current['config'])
                ready=self.store.query('SELECT digest FROM plan_readiness WHERE run_id=?',(old['plan_id'],))[0]
                self.api('POST',f'/api/projects/{self.project}/agent/messages',expected=409,
                    json={'message':'尝试确认已经被替代的旧方案','mode':'execute','plan_id':old['plan_id'],'plan_digest':ready['digest']})
                self.event('stale_confirmation_rejected',plan_id=old['plan_id']);self.execute(current)
            else:current=self.plan(2,roots,parent=initial['task'])
            if stage=='reconnect':self.refresh(current)
            row=self.wait(current,lambda t:t['status']=='succeeded');self.inspect_reuse(current);self.download_and_check(current,row)
            if stage=='completion_race':
                old=self.task(initial['task']);assert old['status']=='cancelled'
                self.event('old_completion_cannot_overwrite_new',task=initial['task'],status=old['status'])
            if stage=='multi_version':
                previous=current;current=self.plan(3,self.revised_roots(previous,3),parent=previous['task'])
                row=self.wait(current,lambda t:t['status']=='succeeded');self.inspect_reuse(current);self.download_and_check(current,row)
            calls=self.store.query('''SELECT operation,state FROM workflow_provider_calls WHERE workflow IN (
                SELECT id FROM agent_workflows WHERE project=? UNION SELECT id FROM agent_turns WHERE project=?)''',(self.project,self.project))
            self.state['external_generation_calls']=len(calls)
            assert not calls,'Unexpected external generation call'
            self.state['status']='succeeded'
        except Exception as exc:
            self.state['status']='failed';self.state['error']=str(exc);self.event('case_failed',error=str(exc)[:300])
        finally:
            try:self.state['cleanup']=self.cleanup()
            except Exception as exc:self.state['cleanup']={'passed':False,'error':str(exc)};self.state['status']='failed'
            self.state['finished_at']=time.time();self.state['elapsed_seconds']=round(time.time()-self.state['started_at'],2)
            self.persist();self.session.close()
            print(json.dumps({'case':self.spec['id'],'status':self.state['status'],'cleanup':self.state['cleanup'].get('passed'),'evidence':str(self.directory)},ensure_ascii=False),flush=True)
        return self.state


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action',choices=['list','run','cleanup'])
    parser.add_argument('--run-id')
    parser.add_argument('--cases',default='all');parser.add_argument('--jobs',type=int,default=1,choices=[1,2])
    parser.add_argument('--repeat',type=int,default=1);parser.add_argument('--timeout',type=int,default=900)
    parser.add_argument('--base',default='http://127.0.0.1/distributed-agent-system')
    args=parser.parse_args();cases=specification()
    if args.cases!='all':
        ids=args.cases.split(',');cases=[c for c in cases if c['id'] in ids]
        if {c['id'] for c in cases}!=set(ids):parser.error('Unknown case identifier')
    if args.action=='list':
        print(json.dumps([{'id':c['id'],'title':c['title'],'stage':c['fixture']['stage'],'level':c['fixture']['level']} for c in cases],ensure_ascii=False,indent=2));return 0
    if args.action=='cleanup':
        if not args.run_id:parser.error('cleanup requires --run-id')
        import requests
        cluster,release=configure()
        from webapp.store import Store
        matched=[]
        for path in (ROOT/'runs').glob('R*/attempt-*/state.json'):
            state=json.loads(path.read_text())
            if state.get('run_id')!=args.run_id:continue
            item=Case.__new__(Case);item.spec=next(c for c in cases if c['id']==state['case'])
            item.args=args;item.cluster=cluster;item.release=release;item.run_id=state['run_id']
            item.directory=path.parent;item.state=state;item.project=state.get('project')
            item.store=Store(cluster.data('node','a'),initialize_schema=False)
            item.session=requests.Session();item.session.trust_env=False
            auth=path.parent/'private-session.json'
            if auth.exists():item.session.cookies.update(json.loads(auth.read_text()))
            try:state['cleanup']=item.cleanup();item.persist();matched.append(state['cleanup'])
            finally:item.session.close()
        if not matched:parser.error('Unknown run ID')
        print(json.dumps(matched,ensure_ascii=False));return 0
    if not 1<=args.repeat<=10:parser.error('repeat must be between 1 and 10')
    cluster,release=configure();results=[]
    for repetition in range(args.repeat):
        with ThreadPoolExecutor(max_workers=args.jobs) as pool:
            results.extend(pool.map(lambda c:Case(c,args,cluster,release).run(),cases))
    report={'release':release,'executed':len(results),'passed':sum(s['status']=='succeeded' and s['cleanup']['passed'] for s in results),
            'results':[{'case':s['case'],'run_id':s['run_id'],'status':s['status'],'error':s.get('error'),'cleanup':s['cleanup']} for s in results]}
    save(DOC/'intervention-latest-run.json',report);print(json.dumps(report,ensure_ascii=False,indent=2))
    return int(report['passed']!=report['executed'])


if __name__=='__main__':raise SystemExit(main())
