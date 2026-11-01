"""Validate generated media inside the task boundary before publishing it."""
import json
import math
from pathlib import Path
import resource
import subprocess
import sys

resource.setrlimit(resource.RLIMIT_AS,(2*1024**3,2*1024**3))
resource.setrlimit(resource.RLIMIT_CPU,(30,30))
resource.setrlimit(resource.RLIMIT_FSIZE,(1024**2,1024**2))
names=json.load(sys.stdin)
if not isinstance(names,list) or len(names)>40:raise ValueError('Invalid output list')
for name in names:
    relative=Path(name)
    if relative.is_absolute() or '..' in relative.parts:raise ValueError('Invalid output path')
    source=Path('edit')/relative
    suffix=source.suffix.lower()
    if suffix in ('.png','.jpg','.jpeg','.webp'):
        from PIL import Image
        with Image.open(source) as image:
            if image.width*image.height>64_000_000:raise ValueError('Image dimensions exceed limit')
            image.verify()
    elif suffix in ('.mp4','.webm','.mov','.mkv','.wav','.mp3','.m4a','.flac','.ogg','.aac','.opus'):
        result=subprocess.run(['ffprobe','-v','error','-show_format','-show_streams','-of','json',str(source)],
            capture_output=True,check=True,timeout=15)
        record=json.loads(result.stdout)
        duration=float(record.get('format',{}).get('duration',0))
        if not math.isfinite(duration) or duration<=0 or not record.get('streams'):
            raise ValueError('Invalid media output')
print('ok')
