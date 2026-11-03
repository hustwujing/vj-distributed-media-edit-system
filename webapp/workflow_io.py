"""Fixed entrypoint inside the project namespace; all output writes are unprivileged."""
import base64
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys


class OutputContractError(ValueError):
    def __init__(self,name,problem,size=None):
        messages={'missing':'Declared output does not exist', 'empty':'Declared output is empty (0 bytes)',
                  'not_file':'Declared output is not a regular file', 'symlink':'Declared output must not be a link'}
        super().__init__(messages[problem]+': '+name)
        self.diagnostic={'code':'output_contract','output':{'name':name,'problem':problem,'bytes':size}}


def execute(request):
    root = Path.cwd().resolve()
    relative = Path(request['output'])
    if relative.is_absolute() or '..' in relative.parts or relative.parts[:2] != ('edit', 'workflows'):
        raise ValueError('Invalid workflow output')
    folder = root
    for part in relative.parts:
        folder = folder / part
        if folder.is_symlink():
            raise ValueError('Output directory must not be a link')
        folder.mkdir(exist_ok=True)
    if request['mode'] == 'import':
        for item in request['files']:
            name = item['name']
            if Path(name).name != name or name in ('.', '..'):
                raise ValueError('Invalid output name')
            with (folder/name).open('xb') as stream:
                stream.write(base64.b64decode(item['data'], validate=True))
            if Path(name).suffix.lower() in ('.png', '.jpg', '.jpeg', '.webp'):
                from PIL import Image
                with Image.open(folder/name) as image:
                    image.verify()
    else:
        argv = [arg.replace('{output}', str(folder)) for arg in request['argv']]
        # Dependency outputs are passed as data; no shell interpolation is used.
        # Bind against the original workspace, not a script's later cwd (or a
        # concat manifest's directory). These remain paths inside the sandbox.
        inputs={role:[str((root/name).resolve()) for name in names]
                for role,names in request.get('inputs',{}).items()}
        env = dict(os.environ, VIDEO_USE_STEP_OUTPUT=str(folder),
                   VIDEO_USE_STEP_INPUTS=json.dumps(inputs, ensure_ascii=False))
        with (folder/'execution.log').open('w') as log:
            result = subprocess.run(argv, env=env, stdout=log, stderr=subprocess.STDOUT)
        if result.returncode:
            error=RuntimeError('本地处理命令执行失败');error.exit_code=result.returncode;raise error
        for name in request['outputs']:
            path = folder/name
            if path.is_symlink():raise OutputContractError(name,'symlink')
            if not path.exists():raise OutputContractError(name,'missing')
            if not path.is_file():raise OutputContractError(name,'not_file')
            size=path.stat().st_size
            if not size:raise OutputContractError(name,'empty',size)
            # Verify common media outputs before a downstream step consumes them.
            if path.suffix.lower() in ('.mp4', '.mov', '.webm', '.mkv', '.wav', '.mp3', '.m4a'):
                data = json.loads(subprocess.check_output(['ffprobe', '-v', 'error', '-show_format',
                                                          '-show_streams', '-of', 'json', str(path)]))
                if not data.get('streams') or float(data.get('format', {}).get('duration', 0)) <= 0:
                    raise ValueError('Invalid media output')
            elif path.suffix.lower() in ('.png', '.jpg', '.jpeg', '.webp'):
                from PIL import Image
                with Image.open(path) as image:
                    image.verify()
        observed=[]
        for name in request.get('observe_outputs',request['outputs']):
            if Path(name).name!=name or name in ('.','..'):raise ValueError('Invalid observation output name')
            path=folder/name
            if path.is_symlink():observed.append({'name':name,'state':'symlink'});continue
            if not path.is_file():observed.append({'name':name,'state':'missing'});continue
            with path.open('rb') as stream:sha=hashlib.file_digest(stream,'sha256').hexdigest()
            observed.append({'name':name,'state':'present','size':path.stat().st_size,'sha256':sha})
        logfile=folder/'execution.log';tail='';truncated=False;size=0
        if not logfile.is_symlink() and logfile.is_file():
            with logfile.open('rb') as stream:
                size=logfile.stat().st_size;truncated=size>8000;stream.seek(max(0,size-8000));tail=stream.read(8000).decode(errors='replace')
        return {'version':1,'returncode':0,'outputs':observed,'stdout_tail':tail,'stdout_bytes':size,'stdout_truncated':truncated,
                'acceptance':'pending_independent_validation'}


def main():
    request=json.load(sys.stdin)
    try:
        receipt=execute(request)
        if request.get('mode')=='local':print(json.dumps(receipt,ensure_ascii=False))
    except Exception as exc:
        log=''
        relative=Path(request.get('output',''))
        if not relative.is_absolute() and '..' not in relative.parts and relative.parts[:2]==('edit','workflows'):
            path=Path.cwd()/relative/'execution.log'
            if not path.is_symlink() and path.is_file():
                with path.open('rb') as stream:
                    stream.seek(max(0,path.stat().st_size-8000))
                    log=stream.read(8000).decode(errors='replace')
        print(json.dumps({'message':str(exc)[-2000:],'exit_code':getattr(exc,'exit_code',None),'log_tail':log,**getattr(exc,'diagnostic',{})},ensure_ascii=False))
        raise SystemExit(1)


if __name__ == '__main__':
    main()
