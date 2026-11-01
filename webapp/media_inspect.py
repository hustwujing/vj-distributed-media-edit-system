"""Fixed metadata decoder inside the API's minimal media namespace."""
import json
import sys
from pathlib import Path
from PIL import Image,ImageOps

with Image.open('/input') as image:
    width,height=image.size
    if width*height>32_000_000 or not 1<=width<=20000 or not 1<=height<=20000:
        raise ValueError('Invalid image dimensions')
    image.verify()
with Image.open('/input') as image:
    if getattr(image,'n_frames',1)!=1:raise ValueError('Animated images are not supported')
    if image.getexif().get(274) in (5,6,7,8):width,height=height,width
    if '--normalize' in sys.argv:
        ImageOps.exif_transpose(image).convert('RGBA').save(sys.stdout.buffer,format='PNG')
    else:
        print(json.dumps({'kind':'image','width':width,'height':height,'size':Path('/input').stat().st_size,
            'mime':Image.MIME.get(image.format,'image/png'),
            'has_alpha':'A' in image.getbands() or 'transparency' in image.info}))
