"""Render a validated canvas snapshot with the same pixels as web exports."""
from pathlib import Path
from PIL import Image,ImageChops,ImageDraw,ImageEnhance,ImageFilter,ImageFont,ImageOps
FONT="/usr/share/fonts/truetype/wqy/wqy-zenhei.ttc"

def render_canvas(document,assets,project):
    from types import SimpleNamespace
    document=SimpleNamespace(**{**document,"layers":[SimpleNamespace(**l) for l in document["layers"]]})
    size=(document.width,document.height);rendered=[];names=[];composite=Image.new('RGBA',size)
    if document.background!='transparent':
        background=Image.new('RGBA',size,document.background);rendered.append(background);names.append('画布背景');composite=Image.alpha_composite(composite,background)
    for l in document.layers:
        if not l.visible:continue
        if l.kind=='adjustment':
            adjusted=adjust(composite,l);surface=Image.blend(composite,adjusted,l.opacity)
            rendered.append(surface);names.append(l.name);composite=surface;continue
        width,height=round(l.width),round(l.height)
        if width*height>32_000_000:raise ValueError('单层尺寸过大')
        if l.kind=='image':
            with Image.open(Path(project)/'sources'/assets[l.asset]['filename']) as img:layer=ImageOps.exif_transpose(img).convert('RGBA').resize((width,height),Image.Resampling.LANCZOS)
        else:
            layer=Image.new('RGBA',(width,height));draw=ImageDraw.Draw(layer)
            if l.kind=='rectangle':draw.rectangle((0,0,width,height),fill=l.color)
            else:
                font=ImageFont.truetype(FONT,l.size);lines=[];spacing=round(l.size*.25)
                for paragraph in l.text.split('\n'):
                    line=''
                    for char in paragraph:
                        if line and draw.textlength(line+char,font=font)>width:lines.append(line);line=char
                        else:line+=char
                    lines.append(line)
                for i,text in enumerate(lines):
                    box=draw.textbbox((0,0),text,font=font,stroke_width=l.stroke_width);tw=box[2]-box[0];x={'left':0,'center':(width-tw)/2,'right':width-tw}[l.text_align];y=i*l.size*1.25
                    if l.shadow_blur:
                        shadow=Image.new('RGBA',layer.size);sd=ImageDraw.Draw(shadow);sd.text((x+l.shadow_x,y+l.shadow_y),text,font=font,fill=l.shadow_color,stroke_width=l.stroke_width,stroke_fill=l.shadow_color);shadow=shadow.filter(ImageFilter.GaussianBlur(l.shadow_blur));layer=Image.alpha_composite(layer,shadow);draw=ImageDraw.Draw(layer)
                    draw.text((x,y),text,font=font,fill=l.color,stroke_width=l.stroke_width,stroke_fill=l.stroke_color)
        layer=adjust(layer,l)
        if l.opacity<1:layer.putalpha(layer.getchannel('A').point(lambda v:round(v*l.opacity)))
        if l.rotation:layer=layer.rotate(-l.rotation,resample=Image.Resampling.BICUBIC,expand=True)
        surface=Image.new('RGBA',size)
        surface.alpha_composite(layer,(round(l.x+(width-layer.width)/2),round(l.y+(height-layer.height)/2)))
        if l.clip_to_below and rendered:surface.putalpha(ImageChops.multiply(surface.getchannel('A'),rendered[-1].getchannel('A')))
        rendered.append(surface);names.append(l.name)
        composite=blend(composite,surface,l.blend_mode)
    if not rendered:rendered=[Image.new('RGBA',size)];names=['空白画布']
    return composite,rendered,names

def adjust(image,l):
    result=image
    if l.brightness!=1:result=ImageEnhance.Brightness(result).enhance(l.brightness)
    if l.contrast!=1:result=ImageEnhance.Contrast(result).enhance(l.contrast)
    if l.saturation!=1:result=ImageEnhance.Color(result).enhance(l.saturation)
    if l.temperature:
        r,g,b,a=result.split();delta=round(45*l.temperature)
        r=r.point(lambda v:max(0,min(255,v+delta)));b=b.point(lambda v:max(0,min(255,v-delta)));result=Image.merge('RGBA',(r,g,b,a))
    if l.black_point or l.white_point!=255 or l.gamma!=1:
        scale=max(1,l.white_point-l.black_point);table=[round((max(0,min(1,(v-l.black_point)/scale))**(1/l.gamma))*255) for v in range(256)];r,g,b,a=result.split();result=Image.merge('RGBA',(r.point(table),g.point(table),b.point(table),a))
    if l.blur:result=result.filter(ImageFilter.GaussianBlur(l.blur))
    if l.sharpen:result=ImageEnhance.Sharpness(result).enhance(1+l.sharpen)
    return result

def blend(base,top,mode):
    if mode=='normal':return Image.alpha_composite(base,top)
    normal=Image.alpha_composite(base,top);alpha=top.getchannel('A');base_alpha=base.getchannel('A');overlap=ImageChops.multiply(base_alpha,alpha);a=base.convert('RGB');b=top.convert('RGB')
    if mode=='multiply':mixed=ImageChops.multiply(a,b)
    elif mode=='screen':mixed=ImageChops.screen(a,b)
    elif mode=='darken':mixed=ImageChops.darker(a,b)
    elif mode=='lighten':mixed=ImageChops.lighter(a,b)
    else:
        low=ImageChops.multiply(a,b).point(lambda v:min(255,v*2));inv=ImageChops.multiply(ImageOps.invert(a),ImageOps.invert(b)).point(lambda v:max(0,255-2*v));mixed=Image.composite(low,inv,a.convert('L').point(lambda v:255 if v<128 else 0))
    rgb=Image.composite(mixed,normal.convert('RGB'),overlap);r,g,b=rgb.split();return Image.merge('RGBA',(r,g,b,normal.getchannel('A')))
