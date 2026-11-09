"""Fixed sandbox decoder for native Harness images sent to the model gateway."""
import base64
import io
import json
import sys


def main():
    from PIL import Image,ImageOps
    request=json.loads(sys.stdin.read(32*1024**2+1))
    raw=base64.b64decode(request['data'],validate=True)
    if len(raw)>20*1024**2:raise ValueError('Image bytes exceed limit')
    with Image.open(io.BytesIO(raw)) as source:
        if source.width*source.height>32_000_000 or getattr(source,'n_frames',1)!=1:
            raise ValueError('Image dimensions exceed limit')
        source.load();image=ImageOps.exif_transpose(source).convert('RGB')
        image.thumbnail((2000,2000));target=io.BytesIO();image.save(target,format='JPEG',quality=85)
    data=target.getvalue()
    if len(data)>2*1024**2:raise ValueError('Normalized image exceeds limit')
    print(json.dumps({'data':base64.b64encode(data).decode()}))


if __name__=='__main__':main()
