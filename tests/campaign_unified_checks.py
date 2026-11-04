"""Independent pixel and temporal checks for the new Run protocol cases."""
from pathlib import Path
import json
try:
    from .campaign_feedback_preview import check as check_feedback
except ImportError:
    from campaign_feedback_preview import check as check_feedback


def check(state,images,videos,database,frame):
    import numpy as np
    from PIL import Image
    errors=[];checks={};case=state['case']
    if case in ('D36','D37'):
        before=state['unified_boundary']['before'];task=next(item for item in before['tasks'] if item['id']=='render')
        filename=task['quality'].get('output_roles',{}).get('primary_image') or task['candidate_result'][0]
        rows=database.query('SELECT manifest FROM execution_step_checkpoints WHERE workflow=? AND step=? AND revision=?',
                            (before['id'],task['id'],task['revision']))
        record=json.loads(rows[0]['manifest'])[filename]
        original_path=database.artifacts.materialize(state['project'],record['artifact'])
        original=Image.open(original_path)
        if not images:return {},['Missing revised result']
        if case=='D36':
            preview=check_feedback(original_path,images[0]['local_file'],'alpha_erode_patch')
            checks['edge_revision_local_patch']=preview
            checks['old_candidate_sha256']=record['sha256']
            if not preview['passed']:errors.append('Edge feedback did not preserve original RGB and apply the declared local Alpha repair')
            rows=database.query('SELECT quality_json,spec_json FROM run_tasks WHERE run_id=? AND selected=1',(state['run_id'],))
            measurements=[]
            for row in rows:
                if json.loads(row['spec_json']).get('semantics',{}).get('mask_repair'):
                    measurements.extend(item for item in json.loads(row['quality_json']).get('checks',[]) if item.get('code')=='mask_change_limit')
            checks['server_mask_measurements']=measurements
            if not measurements or not all(item.get('passed') for item in measurements):errors.append('Frozen mask repair limit was not measured and enforced')
        else:
            old=np.asarray(original.convert('RGB'));expected=np.concatenate((old[:,64:],old[:,:64]),axis=1)
            matches=np.array_equal(expected,np.asarray(Image.open(images[0]['local_file']).convert('RGB')))
            checks['layout_matches_original_swapped_halves']=bool(matches)
            if not matches:errors.append('Revised layout is not the original approved sources in swapped order')
    if case=='D38':
        from webapp.workflow_graph import get
        run=get(database,state['run_id']);identifier='reference'
        replacements={row['old_task_id']:row['new_task_id'] for row in database.query('SELECT old_task_id,new_task_id FROM run_task_replacements WHERE run_id=?',(run['id'],))}
        seen=set()
        while identifier in replacements:
            if identifier in seen:raise ValueError('Replacement cycle')
            seen.add(identifier);identifier=replacements[identifier]
        task=next(item for item in run['tasks'] if item['id']==identifier and item['selected'])
        rocket=next(asset['id'] for asset in database.assets(state['project']) if asset['name']=='rocket.png')
        changed=any(ref.get('asset')==rocket for ref in task['spec']['inputs'].values())
        checks['revised_reference_uses_selected_asset']=changed
        if not changed:errors.append('Reference revision did not bind the newly requested authorized asset')
        calls=database.query('SELECT revision,created FROM workflow_provider_calls WHERE workflow=?',(run['id'],))
        checks['provider_submissions_after_reference_input']=calls
        if len(calls)!=1 or calls[0]['created']<=state['unified_boundary']['input']['accepted_at']:
            errors.append('Video Provider was submitted before the reference change, or submitted more than once')
    if case=='D28':
        if not images:return {},['Missing revised image']
        pixels=np.asarray(Image.open(images[0]['local_file']).convert('RGB'))
        edges=np.concatenate((pixels[:2].reshape(-1,3),pixels[-2:].reshape(-1,3),pixels[:,:2].reshape(-1,3),pixels[:,-2:].reshape(-1,3)))
        fraction=float(((edges[:,0]>245)&(edges[:,1]<10)&(edges[:,2]<10)).mean())
        checks['red_border_fraction']=fraction
        if fraction<.99:errors.append('Revised image lacks the requested two-pixel red inner border')
    if case=='D30':
        if not images:return {},['Missing repaired mask']
        wanted=np.zeros((128,128),dtype=np.uint8);wanted[32:96,32:96]=255
        actual=np.asarray(Image.open(images[0]['local_file']).convert('L'))
        same=actual.shape==wanted.shape and bool(np.array_equal(actual,wanted));checks['independent_mask_pixels_match']=same
        if not same:errors.append('Mask hole repair changed pixels outside the declared defect or failed to fill it')
        rows=database.query('SELECT quality_json,spec_json FROM run_tasks WHERE run_id=? AND selected=1',(state['run_id'],))
        measurements=[]
        for row in rows:
            if json.loads(row['spec_json']).get('semantics',{}).get('mask_repair'):
                measurements.extend(item for item in json.loads(row['quality_json']).get('checks',[]) if item.get('code')=='mask_change_limit')
        checks['server_mask_measurements']=measurements
        if not measurements or not all(item.get('passed') for item in measurements):errors.append('Frozen mask repair limit was not measured and enforced')
    if case=='D31':
        if not videos:return {},['Missing temporal video']
        positions=[]
        for seconds in np.arange(0,3.91,.2):
            pixels=np.asarray(frame(Path(videos[0]['local_file']),float(seconds)))
            selected=(pixels[:,:,0]>170)&(pixels[:,:,1]<90)&(pixels[:,:,2]<90)
            xs=np.nonzero(selected)[1];positions.append({'time':float(seconds),'x':float(xs.mean()) if len(xs)>4 else None,'pixels':int(len(xs))})
        checks['independent_motion_samples']=positions
        if any(item['x'] is None for item in positions):errors.append('Moving square disappeared during the required interval')
        else:
            deltas=np.diff([item['x'] for item in positions])
            if not bool(np.all(deltas>0)):errors.append('Square did not move continuously rightward')
            if float(deltas.max()-deltas.min())>4:errors.append('Square motion contains an abrupt speed or position jump')
    return checks,errors
