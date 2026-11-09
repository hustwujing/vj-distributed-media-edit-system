"""Read-only NVIDIA runtime projection for one scheduler-assigned bwrap GPU."""
from pathlib import Path
import subprocess


def arguments(gpu):
    if type(gpu) is not int or not 0<=gpu<=255:raise ValueError('Invalid assigned GPU index')
    gpu_uuid=subprocess.check_output(['nvidia-smi',f'--id={gpu}','--query-gpu=uuid','--format=csv,noheader'],
        text=True,timeout=10).strip()
    if not gpu_uuid.startswith('GPU-') or '\n' in gpu_uuid:raise ValueError('Invalid assigned GPU identity')
    mounts=[]
    if Path('/proc/driver/nvidia').exists():mounts+=['--ro-bind','/proc/driver/nvidia','/proc/driver/nvidia']
    for name in (f'nvidia{gpu}','nvidiactl','nvidia-uvm','nvidia-uvm-tools'):
        path='/dev/'+name
        if Path(path).exists():mounts+=['--dev-bind',path,path]
    for pattern in ('libnvidia*.so*','libcuda.so*'):
        for path in Path('/usr/lib/x86_64-linux-gnu').glob(pattern):
            if path.is_file():mounts+=['--ro-bind',str(path.resolve()),str(path)]
    parents=set()
    for index,value in enumerate(mounts):
        if value in ('--bind','--ro-bind','--dev-bind'):
            parents.update(parent for parent in Path(mounts[index+2]).parents if str(parent)!='/')
    directories=[]
    for parent in sorted(parents,key=lambda item:(len(item.parts),str(item))):
        directories+=['--perms','0755','--dir',str(parent)]
    return directories+mounts,gpu_uuid
