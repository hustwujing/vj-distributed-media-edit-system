"""Restricted runtime for fixed image tools, separate from generated code."""
import contextlib
import importlib.util
import os
from pathlib import Path
import sys
import time
import json
import subprocess
QUARANTINED_GPU={}

from .agent_sandbox import command, REPO

MODEL_PATHS = (
    '/data/wujing03/miniforge3/envs/face-makeup',
    '/data/wujing03/miniforge3/envs/reference_video_remix_ocr',
    '/data/wujing03/miniforge3/envs/sam3',
    '/data/wujing03/lib-director-vF/src',
    '/data/wujing03/models/sam3', '/data/wujing03/models/ZIM/source',
    '/data/wujing03/models/ZIM/zim-anything-vitl/zim_vit_l_2092',
    '/data/wujing03/models/media-foundation/sam2.1-hiera-large',
    '/data/wujing03/models/media-foundation/paddleocr/.paddleocr/whl',
    '/data/wujing03/wan22-animate/ComfyUI/comfy',
    '/data/wujing03/wan22-animate/ComfyUI/comfy_extras',
    '/data/wujing03/wan22-animate/ComfyUI/folder_paths.py',
    '/data/wujing03/wan22-animate/ComfyUI/comfy_execution',
    '/data/wujing03/wan22-animate/ComfyUI/node_helpers.py',
    '/data/wujing03/image-relighting/models/background_removal/birefnet.safetensors',
)


def allocation_for(pool_root):
    spec=importlib.util.spec_from_file_location('video_use_gpu_manager',os.environ.get('VIDEO_USE_GPU_MANAGER_PATH','/data/wujing03/media-foundation-core/media_core/gpu.py'))
    module=importlib.util.module_from_spec(spec);sys.modules[spec.name]=module;spec.loader.exec_module(module)
    blocked=set()
    for path in (Path(pool_root)/'records').glob('*.json'):
        try:blocked.update(json.loads(path.read_text()).get('gpus',[]))
        except (OSError,ValueError):pass
    from .scheduling import gpu_devices
    return module.GPULease(Path(os.environ.get('VIDEO_USE_GPU_LEASE_ROOT','/data/wujing03/.media-gpu-leases')),
                           tuple(i for i in gpu_devices() if i not in blocked),6000)


@contextlib.contextmanager
def gpu_slot(action, lease, cancel,scope=None,timeout=300):
    if action not in ('segment','remove_bg','segment_sam3','refine_zim'):
        yield None
        return
    allocation=getattr(lease,'gpu_reservation',None) or allocation_for(lease.runtime.identity.pool.root)
    lease.gpu_reservation=None
    deadline=time.monotonic()+timeout
    try:
        gpu=allocation.gpu_index
        while gpu is None:
            gpu=allocation.acquire()
            if gpu is not None:break
            lease.check()
            if cancel.wait(1) or time.monotonic()>deadline:raise RuntimeError('GPU capacity unavailable')
        lease.runtime.identity.save(gpus=sorted(set(lease.runtime.identity.record.get('gpus',[]))|{gpu}))
        yield gpu
    finally:
        if scope is None or scope['stopped']:
            gpu=allocation.gpu_index
            allocation.release()
            lease.runtime.identity.save(gpus=[i for i in lease.runtime.identity.record.get('gpus',[]) if i!=gpu])
        else:
            QUARANTINED_GPU.setdefault(lease.attempt,[]).append(allocation)


def release_reclaimed_gpus(pool):
    for attempt,allocations in list(QUARANTINED_GPU.items()):
        if (pool.root/'runs'/attempt).exists():continue
        for allocation in allocations:allocation.release()
        del QUARANTINED_GPU[attempt]


def image_command(env, socket_path, work, gpu=None,scope=None,timeout=300):
    invocation=command(dict(env,mode='plan',resource_scope=scope),socket_path,
        ['/opt/venv/bin/python','/opt/sandbox_entry.py','--command',
         '/opt/venv/bin/python','-P','-m','webapp.image_tasks_cli','/tool/request.json'])
    position=invocation.index('--chdir')
    from .runtime_image import MANIFEST
    mounts=['--bind',str(work),'/tool',
            '--ro-bind',str(MANIFEST.parent/'tool-app'),'/opt/app']
    action=env.get('image_action')
    if action in ('remove_bg',):selected=[p for p in MODEL_PATHS if any(x in p for x in ('face-makeup','wan22-animate','background_removal'))]
    elif action=='segment':selected=[p for p in MODEL_PATHS if any(x in p for x in ('face-makeup','sam2.1-hiera-large'))]
    elif action in ('segment_sam3','refine_zim'):selected=[p for p in MODEL_PATHS if any(x in p for x in ('sam3','ZIM','lib-director-vF','face-makeup'))]
    elif action in ('ocr','replace_text'):selected=[p for p in MODEL_PATHS if any(x in p for x in ('reference_video_remix_ocr','paddleocr'))]
    elif action:selected=[]
    else:selected=MODEL_PATHS
    for path in selected:
        if Path(path).exists():mounts += ['--ro-bind',path,path]
    if gpu is not None:
        # CUDA ordinals change when only one device node is mounted. Bind a
        # stable GPU UUID, not its host ordinal; metadata is read-only.
        gpu_uuid=subprocess.check_output(['nvidia-smi',f'--id={gpu}','--query-gpu=uuid','--format=csv,noheader'],text=True,timeout=10).strip()
        if not gpu_uuid.startswith('GPU-') or '\n' in gpu_uuid:raise ValueError('Invalid assigned GPU identity')
        if Path('/proc/driver/nvidia').exists():mounts+=['--ro-bind','/proc/driver/nvidia','/proc/driver/nvidia']
        for name in (f'nvidia{gpu}','nvidiactl','nvidia-uvm','nvidia-uvm-tools'):
            path='/dev/'+name
            if Path(path).exists():mounts += ['--dev-bind',path,path]
        for path in Path('/usr/lib/x86_64-linux-gnu').glob('libnvidia*.so*'):
            if path.is_file():mounts+=['--ro-bind',str(path.resolve()),str(path)]
        for path in Path('/usr/lib/x86_64-linux-gnu').glob('libcuda.so*'):
            if path.is_file():mounts+=['--ro-bind',str(path.resolve()),str(path)]
    parents=set()
    for i,value in enumerate(mounts):
        if value in ('--bind','--ro-bind','--dev-bind'):
            parents.update(p for p in Path(mounts[i+2]).parents if str(p)!='/')
    directories=[]
    for parent in sorted(parents,key=lambda p:(len(p.parts),str(p))):directories+=['--perms','0755','--dir',str(parent)]
    mounts=directories+mounts
    invocation[position:position]=mounts
    position=invocation.index('--cap-add')
    invocation[position:position]=[
        '--setenv','PYTHONPATH','/opt/app',
        '--setenv','VIDEO_USE_IMAGE_EDIT_URL','http://127.0.0.1:18766',
        '--setenv','VIDEO_USE_IMAGE_DECOMPOSE_URL','http://127.0.0.1:18766',
        '--setenv','HF_HUB_OFFLINE','1','--setenv','TRANSFORMERS_OFFLINE','1',
        '--setenv','VIDEO_USE_GPU_ASSIGNED','1',
        '--setenv','VIDEO_USE_IMAGE_TIMEOUT',str(max(10,min(3600,int(timeout)))),
        '--setenv','CUDA_VISIBLE_DEVICES',gpu_uuid if gpu is not None else '']
    return invocation
