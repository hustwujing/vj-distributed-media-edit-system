"""Validate preview images INSIDE the project namespace, never in the root launcher."""
import json
import os
import stat
import sys
import warnings
from pathlib import Path

from PIL import Image

FORMATS={'.png':'PNG','.jpg':'JPEG','.jpeg':'JPEG','.webp':'WEBP'}


def inspect_previews(base):
    base=Path(base);records=[];total=0
    if base.is_symlink():raise ValueError('Preview directory cannot be a link')
    with warnings.catch_warnings():
        warnings.simplefilter('error',Image.DecompressionBombWarning)
        for directory,dirs,files,fd in os.fwalk(base,follow_symlinks=False):
            dirs[:]=sorted(d for d in dirs if not d.startswith('.'))
            for name in sorted(files):
                ext=Path(name).suffix.lower()
                if name.startswith('.') or ext not in FORMATS:continue
                try:
                    source_fd=os.open(name,os.O_RDONLY|os.O_NOFOLLOW|os.O_NONBLOCK,dir_fd=fd)
                except OSError:continue
                try:
                    with os.fdopen(source_fd,'rb') as source:
                        info=os.fstat(source.fileno())
                        if not stat.S_ISREG(info.st_mode) or not 0<info.st_size<=32*1024**2:continue
                        with Image.open(source) as image:
                            if image.format!=FORMATS[ext] or image.width*image.height>20_000_000:continue
                            if getattr(image,'n_frames',1)!=1:continue
                            image.load()
                            width,height=image.size
                        if total+info.st_size>128*1024**2:return records
                        records.append({'name':str((Path(directory)/name).relative_to(base)),
                                        'width':width,'height':height,'size':info.st_size})
                        total+=info.st_size
                        if len(records)>=12:return records
                except (OSError,ValueError,Image.DecompressionBombError,Image.DecompressionBombWarning):continue
    return records


if __name__=='__main__':
    print(json.dumps(inspect_previews(sys.argv[1]),ensure_ascii=False))
