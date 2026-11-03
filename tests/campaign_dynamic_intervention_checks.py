"""Independent R-case content and immutable-reuse checks, preserving old pixel checks."""
import json
from pathlib import Path
import shutil


def check(state,spec,images,videos,database,fixture,frame,run_dir):
    if not state['case'].startswith('R'):return {},[]
    import numpy as np
    from PIL import Image,ImageOps,ImageDraw
    from webapp import workflow_graph as graph
    errors=[];checks={};case=state['case'];project=state['project']
    if case in ('R10','R12','R16'):
        previous=[graph.get(database,wid) for wid in state.get('workflow_ids',[])[:-1]]
        checks['prior_deliveries']=[{'workflow_id':w['id'],'status':w['status'],'delivery_count':len(w['delivery'])} for w in previous]
        expected=2 if case=='R16' else 1
        if len(previous)!=expected or any(w['status']!='succeeded' or not w['delivery'] for w in previous):
            errors.append('Required prior successful delivery boundary was not reached')
    reference=run_dir/'references';reference.mkdir(mode=0o700,exist_ok=True)
    sources={}
    for item in state.get('reuse_links',[]):
        rows=database.query('SELECT * FROM workflow_asset_reuse WHERE asset_id=? AND project_id=?',(item['asset_id'],project))
        if len(rows)!=1:errors.append('Missing immutable reuse receipt: '+item['task_id']);continue
        row=rows[0]
        if (row['workflow_id'],row['task_id'],row['revision'],row['sha256'])!=(item['source_workflow'],item['task_id'],item['revision'],item['sha256']):
            errors.append('Reuse identity or hash changed: '+item['task_id']);continue
        file=database.artifacts.materialize(project,row['artifact_id'])
        target=reference/(row['task_id']+Path(row['path']).suffix)
        shutil.copyfile(file,target);target.chmod(0o600);sources[row['task_id']]=target
    expected_reuse={'prepare_b'} if case in ('R05','R06') else {'prepare_a'} if case=='R17' else {'prepare_a','prepare_b'}
    if spec['expect']['kind']=='video':expected_reuse.add('clip')
    if not expected_reuse<=sources.keys():errors.append('Required accepted sources were not reused: '+','.join(sorted(expected_reuse-sources.keys())))
    current=graph.get(database,state['workflow_id'])
    from webapp.operations import get
    operations=[t for t in current['tasks'] if not t['internal'] and t['spec'].get('operation')
                and get(t['spec']['operation'],t['spec'].get('implementation')).executor=='fixed_image']
    checks['recomputed_image_operations']=len(operations)
    if case in ('R05','R06'):
        if len(operations)!=1:errors.append('Exactly one changed branch must be recomputed')
        else:
            task=operations[0];brightness=task['spec']['parameters'].get('parameters',{}).get('brightness')
            if brightness!=(1.35 if case=='R05' else 1.1):errors.append('Changed brightness does not match requested value')
            asset=next((a for a in database.assets(project) if a['id']==task['spec']['inputs'].get('source',{}).get('asset')),None)
            if not asset or asset['name']!=('portrait.png' if case=='R05' else 'rocket.png'):errors.append('Recomputed branch uses wrong source')
            records=database.query('SELECT manifest FROM execution_step_checkpoints WHERE workflow=? AND step=? AND revision=?',(current['id'],task['id'],task['revision']))
            logical=task['quality'].get('output_roles',{}).get('primary_image')
            record=json.loads(records[0]['manifest']).get(logical) if records else None
            if record:
                target=reference/'recomputed-prepare-a.png';shutil.copyfile(database.artifacts.materialize(project,record['artifact']),target);sources['prepare_a']=target
            else:errors.append('Recomputed branch lacks accepted immutable output')
    elif operations:errors.append('Already accepted image operations ran again')
    checks['reused_sources']={key:str(value.relative_to(run_dir)) for key,value in sources.items()}
    by_name={Path(item['name']).name:item for item in images+videos}
    required={'renamed.png'} if case=='R01' else {'final.webp'} if case=='R02' else {'final.png','final.jpg'} if case=='R04' else {'final.png','thumbnail.png'} if case in ('R10','R16') else {'final.mp4','cover.png'} if spec['expect']['kind']=='video' else {'final.png'} if case!='R17' else set()
    if not required<=by_name.keys():errors.append('Missing named delivery: '+','.join(sorted(required-by_name.keys())))
    if case!='R17':
        primary=next((item for item in images+videos if item.get('is_primary')),None)
        primary_name='renamed.png' if case=='R01' else 'final.webp' if case=='R02' else 'final.mp4' if spec['expect']['kind']=='video' else 'final.png'
        if not primary or Path(primary['name']).name!=primary_name:errors.append('Primary delivery does not match the confirmed name: '+primary_name)
    if 'prepare_a' not in sources:return checks,errors
    a=Image.open(sources['prepare_a']).convert('RGB');b=Image.open(sources['prepare_b']).convert('RGB') if 'prepare_b' in sources else a
    width,height=spec['expect']['width'],spec['expect']['height']
    expected=Image.new('RGB',(width,height))
    if case=='R17':expected=ImageOps.fit(a,(width,height))
    else:
        for index,im in enumerate([b,a] if case=='R03' else [a,b]):expected.paste(ImageOps.fit(im,(width//2,height)),(index*(width//2),0))
    if case=='R11':ImageDraw.Draw(expected).rectangle((0,0,width-1,height-1),outline='#FF0000',width=4)
    pixel_checks=[]
    for item in images:
        name=Path(item['name']).name
        if required and name not in required:continue
        actual=Image.open(item['local_file']).convert('RGB')
        wanted=expected.resize((32,16),Image.Resampling.LANCZOS) if name=='thumbnail.png' else ImageOps.fit(b if case=='R07' else a,(128,64)) if name=='cover.png' else expected
        if actual.size!=wanted.size:errors.append('Independent pixel reference size mismatch: '+name);continue
        mae=float(np.abs(np.asarray(actual,dtype=float)-np.asarray(wanted,dtype=float)).mean())
        pixel_checks.append({'name':name,'mae':mae,'reference':'accepted upstream objects'})
        if mae>=(12 if Path(name).suffix in ('.jpg','.webp') else .01):errors.append('Independent pixel reference mismatch: '+name)
    checks['reuse_pixel_checks']=pixel_checks
    if videos and 'clip' in sources:
        wanted=ImageOps.fit(frame(sources['clip'],.5),(width,height))
        for item in videos:
            actual=np.asarray(frame(Path(item['local_file']),.5),dtype=float)
            mae=float(np.abs(actual[30:]-np.asarray(wanted,dtype=float)[30:]).mean())
            checks['reuse_video_source_frame_mae']=mae
            if mae>=18:errors.append('Video content differs from accepted clip')
            if case=='R08':
                text=actual[2:26,2:34];yellow=int(((text[:,:,0]>150)&(text[:,:,1]>140)&(text[:,:,2]<110)).sum())
                checks['yellow_caption_pixels']=yellow
                if yellow<12:errors.append('Yellow QA caption missing')
    if case=='R14' and not state.get('output_completion_boundary'):errors.append('Actual output completion boundary was not observed')
    if case=='R15' and not any('Re-entered' in value for value in state.get('browser_assertions',[])):errors.append('Browser re-entry was not verified')
    if case=='R16' and len(state.get('workflow_ids',[]))!=3:errors.append('Three actual workflow versions were not executed')
    return checks,errors
