"""Fixed SAM3/ZIM runner. Paths and model locations are host-selected only."""
from __future__ import annotations
import json,os,shutil,socket,subprocess,sys,time
from pathlib import Path
from urllib.request import Request,urlopen

SAM3_PYTHON='/data/wujing03/miniforge3/envs/sam3/bin/python'
ZIM_PYTHON='/data/wujing03/miniforge3/envs/face-makeup/bin/python'
PROJECT=Path('/data/wujing03/lib-director-vF')
SOURCE=PROJECT/'src'
SAM3_CHECKPOINT=Path('/data/wujing03/models/sam3/sam3.pt')
ZIM_CHECKPOINT=Path('/data/wujing03/models/ZIM/zim-anything-vitl/zim_vit_l_2092')

class NoSelectionError(RuntimeError):
    """Inference completed, but produced no object selection."""

def request_json(url,payload,timeout=300):
    request=Request(url,data=json.dumps(payload).encode(),headers={'Content-Type':'application/json'},method='POST')
    with urlopen(request,timeout=timeout) as response:return json.load(response)

def wait_health(endpoint,process):
    deadline=time.monotonic()+45
    while time.monotonic()<deadline:
        if process.poll() is not None:raise RuntimeError('SAM3 worker exited during startup')
        try:
            with urlopen(endpoint+'/health',timeout=1) as response:
                if json.load(response).get('status')=='ok':return
        except Exception:time.sleep(.25)
    raise RuntimeError('SAM3 worker startup timeout')

def run_sam3(req,work):
    with socket.socket() as sock:sock.bind(('127.0.0.1',0));port=sock.getsockname()[1]
    endpoint=f'http://127.0.0.1:{port}';env={**os.environ,'PYTHONPATH':str(SOURCE)+':'+str(Path('/data/wujing03/models/sam3')),'HF_HUB_OFFLINE':'1','TRANSFORMERS_OFFLINE':'1'}
    for key in ('PYTHONHOME','LD_LIBRARY_PATH','PYTHONPYCACHEPREFIX'):env.pop(key,None)
    log=(work/'sam3.log').open('w');process=subprocess.Popen([SAM3_PYTHON,'-u','-m','libdirector_vf.sam3_worker','--bind','127.0.0.1','--port',str(port),'--checkpoint',str(SAM3_CHECKPOINT)],cwd=PROJECT,env=env,stdout=log,stderr=subprocess.STDOUT)
    try:
        wait_health(endpoint,process);output=work/'sam3';output.mkdir()
        payload={'image':req['source'],'output_dir':str(output),'draft_id':'selection'}
        if req.get('prompt'):
            result=request_json(endpoint+'/segment/text',{**payload,'labels':[req['prompt']],'build_material_groups':False,'confidence':req.get('confidence',.35)})
            data=json.loads(Path(result['detections']).read_text());records=[r for r in data['detections'] if r.get('recommended')]
            if not records:records=data['detections']
            if not records:raise NoSelectionError('SAM3 did not find the described object')
            record=max(records,key=lambda r:(float(r.get('score',0)),int(r.get('mask_pixels',0))));mask=output/record['mask'];preview=output/record['preview']
        else:
            box=req.get('box');points=req.get('points') or []
            payload['box']=[box[0],box[1],box[0]+box[2],box[1]+box[3]] if box else None
            if points:payload['groups']=[{'points':[{'x':p[0],'y':p[1],'label':1} for p in points]}]
            result=request_json(endpoint+'/segment/points',payload);mask=output/result['mask'];preview=output/result['preview']
        shutil.copyfile(mask,work/'raw-mask.png');shutil.copyfile(preview,work/'sam3-preview.jpg')
    except NoSelectionError:
        raise
    except Exception as exc:
        log.flush()
        raise RuntimeError(str(exc)+': '+(work/'sam3.log').read_text(errors='replace')[-1600:]) from exc
    finally:
        try:request_json(endpoint+'/shutdown',{},10)
        except Exception:pass
        try:process.wait(timeout=15)
        except subprocess.TimeoutExpired:process.kill();process.wait()
        log.close()

def run_zim(req,work,raw_mask):
    output=work/'zim';output.mkdir(exist_ok=True);record=output/'result.json';env={**os.environ,'PYTHONPATH':str(SOURCE)+':/data/wujing03/models/ZIM/source'}
    for key in ('PYTHONHOME','LD_LIBRARY_PATH','PYTHONPYCACHEPREFIX'):env.pop(key,None)
    command=[ZIM_PYTHON,'-u','-m','libdirector_vf.zim_refine','--checkpoint',str(ZIM_CHECKPOINT),'--image',req['source'],'--output-dir',str(output),'--raw-mask',str(raw_mask),'--label',req.get('prompt') or 'object','--id','selection','--source','video-use','--output',str(record)]
    result=subprocess.run(command,cwd=PROJECT,env=env,capture_output=True,text=True,timeout=300)
    (work/'zim.log').write_text(result.stdout+'\n'+result.stderr)
    if result.returncode:raise RuntimeError('ZIM refinement failed: '+result.stderr[-1600:])
    data=json.loads(record.read_text());shutil.copyfile(output/data['alpha'],Path(req['output']));shutil.copyfile(output/data['preview'],work/'zim-preview.jpg')
    return data

def main():
    request_path=Path(sys.argv[1]);req=json.loads(request_path.read_text());work=request_path.parent
    raw=Path(req['mask']) if req.get('mask') else work/'raw-mask.png'
    if not req.get('mask'):run_sam3(req,work)
    if req.get('refine',True):metadata=run_zim(req,work,raw)
    else:shutil.copyfile(raw,req['output']);metadata={'segmenter':'sam3','matting':'none'}
    Path(req['metadata']).write_text(json.dumps(metadata,ensure_ascii=False,indent=2))

if __name__=='__main__':
    try:main()
    except NoSelectionError:
        # A fixed, bounded outcome survives log truncation without parsing prose.
        Path(sys.argv[1]).parent.joinpath('model-outcome.json').write_text(
            json.dumps({'schema':1,'code':'no_selection'}))
        raise SystemExit(2)
