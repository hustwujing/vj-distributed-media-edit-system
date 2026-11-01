"""Per-task service sessions, bounded downloads and cancellation of upstream jobs."""
import contextlib
import json
import os
import re
import signal
import subprocess
import threading
import time
from pathlib import Path

import requests

ENDPOINTS={'edit':(os.environ.get('VIDEO_USE_IMAGE_EDIT_URL','http://127.0.0.1:8911'),'target_image'),
           'decompose':(os.environ.get('VIDEO_USE_IMAGE_DECOMPOSE_URL','http://127.0.0.1:8912'),'source_image')}

# Contract of this adapter's image-editor service, not a global image/task rule.
EDITOR_CONTRACT={'provider_protocol':'qwen-image-editor-v1','input_min_side':256,
                 'input_max_pixels':64_000_000,'maximum_images_including_selection_guide':3,
                 'assembled_prompt_max_length':2000}


def check_image_request(contract,sizes,prompt):
    maximum=contract['maximum_images_including_selection_guide']
    minimum=contract['input_min_side'];pixels=contract['input_max_pixels']
    prompt_limit=contract['assembled_prompt_max_length']
    if not 1<=len(sizes)<=maximum:
        raise ValueError(f'当前图片服务最多接受{maximum}张输入图，包含主图、参考图和选区指示图')
    for index,(width,height) in enumerate(sizes,1):
        if min(width,height)<minimum:
            raise ValueError(f'当前图片服务要求输入{index}的短边至少为{minimum}像素，请调整本地输入准备')
        if width*height>pixels:
            raise ValueError(f'当前图片服务要求输入{index}不超过{pixels}像素')
    if not prompt.strip() or len(prompt)>prompt_limit:
        raise ValueError(f'当前图片服务完整指令长度必须为1到{prompt_limit}字符，包含服务端选区说明')


def remote_images(action,source,output,params,references=(),cancel=None,progress=None):
    if action=='edit':
        from PIL import Image
        sizes=[]
        for path in [source,*references]:
            with Image.open(path) as im:sizes.append(im.size)
        check_image_request(EDITOR_CONTRACT,sizes,params.get('prompt',''))
    base,field=ENDPOINTS[action];output=Path(output);output.mkdir(parents=True,exist_ok=True)
    cancel=cancel or threading.Event();progress=progress or (lambda text:print(text,flush=True))
    with requests.Session() as session, contextlib.ExitStack() as stack:
        session.trust_env=False
        files=[(field,(Path(source).name,stack.enter_context(Path(source).open('rb')),'image/png'))]
        for path in references:files.append(('reference_images',(Path(path).name,stack.enter_context(Path(path).open('rb')),'image/png')))
        job_id=None;done=False
        try:
            if cancel.is_set():raise RuntimeError('图片任务已取消')
            response=session.post(base+'/api/jobs',files=files,data=params,timeout=(10,120),allow_redirects=False)
            if response.status_code!=202:raise RuntimeError(f'图片服务未接受任务（HTTP {response.status_code}）')
            job_id=response.json()['id']
            if not re.fullmatch(r'[A-Za-z0-9_-]{1,100}',job_id):raise RuntimeError('图片服务任务 ID 无效')
            start=time.monotonic();last=None
            while time.monotonic()-start<1500:
                if cancel.is_set():raise RuntimeError('图片任务已取消')
                response=session.get(base+'/api/jobs/'+job_id,timeout=(10,30),allow_redirects=False);response.raise_for_status();record=response.json()
                stage=(record.get('stage'),record.get('progress'))
                if stage!=last:progress(f'{stage[0] or "处理中"} · {stage[1] or 0}%');last=stage
                if record['status'] in ('failed','cancelled'):
                    from .generation_errors import task_failure
                    raise task_failure(record)
                if record['status']=='succeeded':break
                cancel.wait(1)
            else:raise RuntimeError('图片任务超时，已请求取消')
            artifacts=[];total=0
            for i,item in enumerate(record.get('artifacts',[])[:30]):
                artifact_id=item.get('id','')
                if not re.fullmatch(r'[A-Za-z0-9_-]{1,100}',artifact_id):continue
                name=Path(item.get('name','')).name;ext=Path(name).suffix.lower()
                if ext not in {'.png','.jpg','.jpeg','.webp','.psd','.ora','.json','.zip','.tiff'}:continue
                # Server-chosen fixed route, never an arbitrary URL from output data.
                destination=output/f'{i:02}-{name}'
                with session.get(base+f'/api/jobs/{job_id}/artifacts/{artifact_id}',stream=True,timeout=(10,90),allow_redirects=False) as response:
                    response.raise_for_status();size=0
                    with destination.open('xb') as f:
                        for chunk in response.iter_content(1024*1024):
                            if cancel.is_set():raise RuntimeError('图片任务已取消')
                            size+=len(chunk);total+=len(chunk)
                            if size>256*1024**2 or total>512*1024**2:raise RuntimeError('图片服务结果超过下载限制')
                            f.write(chunk)
                artifacts.append({'name':name,'path':destination,'kind':item.get('kind'),'type':item.get('media_type','')})
            done=True;return artifacts
        finally:
            if job_id and not done:
                with contextlib.suppress(requests.RequestException):session.post(base+f'/api/jobs/{job_id}/cancel',timeout=10,allow_redirects=False)


def model_image(action,source,output,parameters,work,cancel=None):
    """Use existing pinned local weights in separate Python environments."""
    cancel=cancel or threading.Event();work=Path(work)
    request=work/f'{action}-request.json';request.write_text(json.dumps({'source':str(source),'output':str(output),**parameters}))
    interpreter=(str(Path(__file__).resolve().parents[1]/'.venv/bin/python') if action in ('segment_sam3','refine_zim') else '/data/wujing03/miniforge3/envs/'+('reference_video_remix_ocr' if action=='ocr' else 'face-makeup')+'/bin/python')
    env={k:v for k,v in os.environ.items() if not any(s in k.upper() for s in ('KEY','TOKEN','SECRET','PASSWORD'))}
    if not interpreter.startswith('/opt/app/'):
        for name in ('PYTHONHOME','PYTHONPYCACHEPREFIX','LD_LIBRARY_PATH'):env.pop(name,None)
    env.update(HF_HUB_OFFLINE='1',TRANSFORMERS_OFFLINE='1',XDG_CACHE_HOME=str(work/'cache'),TORCH_HOME=str(work/'cache'),OMP_NUM_THREADS='4',OPENBLAS_NUM_THREADS='4')
    if action=='ocr':env['OMP_NUM_THREADS']='1';env['OPENBLAS_NUM_THREADS']='1'
    lease=None
    if action!='ocr' and os.environ.get('VIDEO_USE_GPU_ASSIGNED') != '1':
        import sys,importlib.util
        spec=importlib.util.spec_from_file_location('updream_shared_gpu','/data/wujing03/media-foundation-core/media_core/gpu.py')
        module=importlib.util.module_from_spec(spec);sys.modules[spec.name]=module;spec.loader.exec_module(module)
        GPULease=module.GPULease
        lease=GPULease(Path('/data/wujing03/.media-gpu-leases'),tuple(range(8)),6000)
        start=time.monotonic()
        while (gpu:=lease.acquire()) is None:
            if cancel.wait(2) or time.monotonic()-start>300:raise RuntimeError('暂无可用 GPU，请稍后重试')
        env['CUDA_VISIBLE_DEVICES']=str(gpu)
    process=None
    try:
        command=([interpreter,'-u',str(Path(__file__).with_name('sam3_zim.py')),str(request)] if action in ('segment_sam3','refine_zim') else [interpreter,'-u',str(Path(__file__).with_name('image_models.py')),action,str(request)])
        with (work/'model.log').open('w') as log:
            process=subprocess.Popen(command,stdout=log,stderr=subprocess.STDOUT,env=env,start_new_session=True,cwd=work)
            start=time.monotonic()
            while process.poll() is None:
                if cancel.wait(.3) or time.monotonic()-start>max(10,min(3600,int(os.environ.get('VIDEO_USE_IMAGE_TIMEOUT','300')))):raise RuntimeError('图片模型任务已取消或超时')
            if process.returncode:raise RuntimeError('图片模型执行失败：'+(work/'model.log').read_text(errors='replace')[-1200:])
    finally:
        if process and process.poll() is None:
            os.killpg(process.pid,signal.SIGTERM)
            try:process.wait(timeout=5)
            except subprocess.TimeoutExpired:os.killpg(process.pid,signal.SIGKILL);process.wait()
        if lease:lease.release()
