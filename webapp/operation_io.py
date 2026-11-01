"""Fixed image preparation and completion inside an unprivileged namespace."""
import json
import sys
from pathlib import Path
from PIL import Image, ImageOps, ImageDraw


def main():
    request=json.load(sys.stdin)
    output=Path(request['output']);output.mkdir(parents=True,exist_ok=True)
    mode=request['mode']
    if mode=='prepare_image':
        task=request['task']
        with Image.open(request['source']) as im:image=ImageOps.exif_transpose(im).convert('RGBA')
        image.save(output/'source.png')
        mask=None
        if request.get('mask'):
            with Image.open(request['mask']) as im:mask=im.convert('L')
        elif task.get('box'):
            x,y,w,h=task['box'];mask=Image.new('L',image.size);ImageDraw.Draw(mask).rectangle((x,y,x+w-1,y+h-1),fill=255)
        if mask:
            mask.save(output/'mask.png')
            tint=Image.new('RGBA',image.size,(255,32,32,0));tint.putalpha(mask.point(lambda p:round(p*.4)))
            Image.alpha_composite(image,tint).save(output/'selection.png')
    elif mode=='composite_image':
        with Image.open(request['source']) as im:source=im.convert('RGBA')
        with Image.open(request['generated']) as im:generated=im.convert('RGBA').resize(source.size)
        with Image.open(request['mask']) as im:mask=im.convert('L')
        Image.composite(generated,source,mask).save(output/'image.png')
    elif mode=='fit_image':
        with Image.open(request['source']) as im:image=ImageOps.exif_transpose(im).convert('RGBA')
        size=(request['width'],request['height'])
        if request['fit']=='cover':result=ImageOps.fit(image,size,Image.Resampling.LANCZOS)
        else:
            fitted=ImageOps.contain(image,size,Image.Resampling.LANCZOS)
            result=Image.new('RGBA',size,request['background'])
            result.alpha_composite(fitted,((size[0]-fitted.width)//2,(size[1]-fitted.height)//2))
        result.save(output/'image.png')
    else:raise ValueError('Unknown fixed operation')

if __name__=='__main__':main()
