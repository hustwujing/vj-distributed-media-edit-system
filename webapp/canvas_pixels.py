"""Fixed unprivileged decoder: stdin bytes -> bounded, normalized PNG on stdout."""
import io
import resource
import sys
import warnings
from PIL import Image,ImageOps

resource.setrlimit(resource.RLIMIT_AS,(1024**3,1024**3))
resource.setrlimit(resource.RLIMIT_CPU,(15,15))
resource.setrlimit(resource.RLIMIT_FSIZE,(128*1024**2,128*1024**2))
warnings.simplefilter('error',Image.DecompressionBombWarning)
Image.MAX_IMAGE_PIXELS=16_777_216
data=sys.stdin.buffer.read(24*1024**2+1)
if len(data)>24*1024**2:raise ValueError('Image too large')
with Image.open(io.BytesIO(data)) as im:
    if im.format not in ('PNG','JPEG','WEBP') or getattr(im,'n_frames',1)!=1 or max(im.size)>8192 or im.width*im.height>16_777_216:raise ValueError('Invalid image dimensions or format')
    image=ImageOps.exif_transpose(im).convert('RGBA')
    if '--model' in sys.argv:image.thumbnail((2048,2048))
    image.save(sys.stdout.buffer,format='PNG')
