"""Versioned image documents and deterministic layer rendering."""
import io
import re
import zipfile
from pathlib import Path
from typing import Literal
from xml.etree import ElementTree as ET

from PIL import Image
from pydantic import BaseModel,ConfigDict,Field,model_validator
from helpers.canvas_render import FONT

def edit_prompt(action,prompt='',selected=False):
    if action=='erase':
        prompt=('完整移除选区内的目标物体或文字，包括全部笔画、边缘和残影，用周围合理的背景自然补全。'
                '不要重画、移动、缩放或保留要消除的内容。 '+prompt.strip())
    if selected:
        prompt=('图1是待编辑原图，图2仅用红色覆盖标明选区。只修改选区，保持原图构图、尺寸、'
                '物体位置及其他内容不变；不要在结果中画红色标记。 '+prompt)
    return prompt.strip()


class Layer(BaseModel):
    model_config=ConfigDict(extra='forbid',allow_inf_nan=False)
    id:str=Field(min_length=1,max_length=80)
    name:str=Field(default='图层',max_length=100)
    kind:Literal['image','text','rectangle','adjustment']='image'
    asset:str|None=None
    text:str=Field(default='',max_length=2000)
    x:float=Field(default=0,ge=-8192,le=8192)
    y:float=Field(default=0,ge=-8192,le=8192)
    width:float=Field(default=640,ge=1,le=8192)
    height:float=Field(default=480,ge=1,le=8192)
    rotation:float=Field(default=0,ge=-360,le=360)
    opacity:float=Field(default=1,ge=0,le=1)
    visible:bool=True
    locked:bool=False
    size:int=Field(default=48,ge=8,le=400)
    color:str=Field(default='#ffffff',pattern=r'^#[0-9a-fA-F]{6}$')
    blend_mode:Literal['normal','multiply','screen','overlay','darken','lighten']='normal'
    brightness:float=Field(default=1,ge=.2,le=3)
    contrast:float=Field(default=1,ge=.2,le=3)
    saturation:float=Field(default=1,ge=0,le=3)
    temperature:float=Field(default=0,ge=-1,le=1)
    gamma:float=Field(default=1,ge=.1,le=5)
    black_point:int=Field(default=0,ge=0,le=254)
    white_point:int=Field(default=255,ge=1,le=255)
    blur:float=Field(default=0,ge=0,le=50)
    sharpen:float=Field(default=0,ge=0,le=5)
    clip_to_below:bool=False
    stroke_color:str=Field(default='#000000',pattern=r'^#[0-9a-fA-F]{6}$')
    stroke_width:int=Field(default=0,ge=0,le=30)
    shadow_color:str=Field(default='#000000',pattern=r'^#[0-9a-fA-F]{6}$')
    shadow_blur:int=Field(default=0,ge=0,le=50)
    shadow_x:int=Field(default=0,ge=-200,le=200)
    shadow_y:int=Field(default=0,ge=-200,le=200)
    text_align:Literal['left','center','right']='left'

class Document(BaseModel):
    model_config=ConfigDict(extra='forbid',allow_inf_nan=False)
    revision:int=Field(default=0,ge=0)
    width:int=Field(default=1280,ge=16,le=4096)
    height:int=Field(default=720,ge=16,le=4096)
    background:str=Field(default='transparent',pattern=r'^(transparent|#[0-9a-fA-F]{6})$')
    layers:list[Layer]=Field(default_factory=list,max_length=40)

class ImageTask(BaseModel):
    model_config=ConfigDict(extra='forbid',allow_inf_nan=False)
    action:Literal['export','segment','segment_sam3','refine_zim','remove_bg','local_edit','erase','ocr','replace_text','decompose','generate','refine_mask','perspective','product_layout','color_match','local_adjust','batch_export']
    asset:str|None=None
    references:list[str]=Field(default_factory=list,max_length=2)
    mask:str|None=None
    document:Document|None=None
    box:list[int]|None=Field(default=None,min_length=4,max_length=4)
    points:list[list[int]]=Field(default_factory=list,max_length=16)
    prompt:str=Field(default='',max_length=5000)
    text:str=Field(default='',max_length=500)
    size:int=Field(default=32,ge=8,le=300)
    color:str=Field(default='#ffffff',pattern=r'^#[0-9a-fA-F]{6}$')
    quality:Literal['fast','standard','high']='fast'
    layer_count:Literal[3,4,6,8]=3
    format:Literal['png','jpg','webp','psd','ora']='png'
    parameters:dict=Field(default_factory=dict)

    @model_validator(mode='after')
    def selection_mode(self):
        if self.action=='segment_sam3':
            self.prompt=self.prompt.strip()
            if self.prompt and (self.box is not None or self.points):
                raise ValueError('SAM3 文字选择与点框选择不能同时使用；使用点或框时请将 prompt 留空')
            if self.box is not None and self.points:
                raise ValueError('当前 SAM3 点选择与框选择不能同时使用；请选择其中一种，避免点提示被忽略')
        return self


def check_document(document,assets):
    if len({l.id for l in document.layers})!=len(document.layers):raise ValueError('图层 ID 不能重复')
    for layer in document.layers:
        if layer.kind=='image' and (layer.asset not in assets or assets[layer.asset]['metadata']['kind']!='image'):raise ValueError('图层引用的图片不属于当前项目')
        if layer.black_point>=layer.white_point:raise ValueError('图层黑场必须小于白场')


def check_task(task,assets):
    for value in [task.asset,task.mask,*task.references]:
        if value is not None and (value not in assets or assets[value]['metadata']['kind']!='image'):raise ValueError('请选择当前项目的图片素材')
    if task.action=='export':
        if task.document is None:raise ValueError('请先创建画布')
        check_document(task.document,assets);return
    if task.asset is None:raise ValueError('请先选择图片图层')
    meta=assets[task.asset]['metadata'];w,h=meta['width'],meta['height']
    if task.box is not None:
        x,y,bw,bh=task.box
        if min(x,y)<0 or min(bw,bh)<2 or x+bw>w or y+bh>h:raise ValueError('选区超出图片范围')
    for p in task.points:
        if len(p)!=2 or any(isinstance(v,bool) or not isinstance(v,int) for v in p) or not 0<=p[0]<w or not 0<=p[1]<h:raise ValueError('选择点不在图片范围内')
    if task.action=='segment' and task.box is None and not task.points:raise ValueError('请先点击或框选主体')
    if task.action=='segment_sam3' and task.box is None and not task.points and not task.prompt.strip():raise ValueError('SAM3 需要文字、点或框提示')
    if task.action=='segment_sam3' and len(task.prompt)>120:raise ValueError('SAM3 目标名称不能超过120字')
    if task.action=='segment_sam3' and task.prompt and re.search(r'[\u3400-\u9fff]',task.prompt):raise ValueError('当前 SAM3 文字提示需使用简短英文；中文需求请交给 Agent 自动翻译，或改用点/框选择')
    if task.action=='refine_zim' and not task.mask:raise ValueError('ZIM 精修需要已有蒙版')
    if task.action=='perspective' and len(task.points)!=4:raise ValueError('透视校正需要按顺序选择四个角点')
    if task.action in ('erase','local_edit','replace_text') and not task.mask and task.box is None:raise ValueError('请先框选或选择蒙版')
    if task.mask:
        mm=assets[task.mask]['metadata']
        if (mm['width'],mm['height'])!=(w,h):raise ValueError('蒙版必须与源图片尺寸一致')
    if task.action in ('local_edit','generate') and not task.prompt.strip():raise ValueError('请描述要生成的内容')
    if task.action=='replace_text' and not task.text.strip():raise ValueError('请输入替换文字')
    allowed={'segment_sam3':{'refine','confidence'},'refine_zim':set(),'refine_mask':{'grow','feather','threshold','remove_small','fill_holes'},'product_layout':{'width','height','background','margin','shadow','shadow_blur','shadow_x','shadow_y'},'color_match':{'strength'},'local_adjust':{'brightness','contrast','saturation','temperature','gamma','black_point','white_point','sharpen','blur','auto_white_balance','auto_levels'},'batch_export':{'width','height','fit','background','format','quality','watermark'}}.get(task.action,set())
    if set(task.parameters)-allowed:raise ValueError('此工具包含未定义的高级参数')
    if task.action=='color_match' and not task.references:raise ValueError('色彩匹配需要选择一张参考图片')
    if task.action in ('product_layout','batch_export'):
        for key in ('width','height'):
            value=task.parameters.get(key)
            if isinstance(value,bool) or not isinstance(value,int) or not 16<=value<=4096:raise ValueError('输出尺寸需为16–4096的整数')
        if not re.fullmatch(r'#[0-9a-fA-F]{6}',str(task.parameters.get('background','#ffffff'))):raise ValueError('背景颜色需为 #RRGGBB')
    def numeric(key,low,high,default=0):
        value=task.parameters.get(key,default)
        if isinstance(value,bool) or not isinstance(value,(int,float)) or not low<=value<=high:raise ValueError(f'{key} 参数超出范围')
    if task.action=='refine_mask':
        for key,low,high,default in [('grow',-100,100,0),('feather',0,100,0),('threshold',0,255,128),('remove_small',0,1_000_000,0)]:numeric(key,low,high,default)
        if 'fill_holes' in task.parameters and not isinstance(task.parameters['fill_holes'],bool):raise ValueError('fill_holes 参数无效')
    if task.action=='local_adjust':
        for key,low,high,default in [('brightness',.2,3,1),('contrast',.2,3,1),('saturation',0,3,1),('temperature',-1,1,0),('gamma',.1,5,1),('black_point',0,254,0),('white_point',1,255,255),('sharpen',0,5,0),('blur',0,50,0)]:numeric(key,low,high,default)
        if task.parameters.get('black_point',0)>=task.parameters.get('white_point',255):raise ValueError('黑场必须小于白场')
        for key in ('auto_white_balance','auto_levels'):
            if key in task.parameters and not isinstance(task.parameters[key],bool):raise ValueError(key+' 参数无效')
    if task.action=='color_match':numeric('strength',0,1,1)
    if task.action=='segment_sam3':
        numeric('confidence',.01,.99,.35)
        if 'refine' in task.parameters and not isinstance(task.parameters['refine'],bool):raise ValueError('refine 参数无效')
    if task.action in ('product_layout','batch_export'):
        numeric('margin',0,4096,0);numeric('quality',40,100,90)
        if task.parameters.get('format','png') not in ('png','jpg','webp') or task.parameters.get('fit','contain') not in ('contain','cover'):raise ValueError('导出格式或填充方式无效')


def render_document(document,assets,project):
    from helpers.canvas_render import render_canvas
    return render_canvas(document.model_dump(),assets,project)


def export_document(document,assets,project,output,fmt):
    output=Path(output);image,layers,names=render_document(document,assets,project)
    image.save(output/'image.png');(output/'document.json').write_text(document.model_dump_json(indent=2))
    files=['image.png','document.json']
    if fmt in ('jpg','webp'):
        target=output/('image.'+fmt);flat=Image.new('RGBA',image.size,'white');flat.alpha_composite(image)
        (flat.convert('RGB') if fmt=='jpg' else image).save(target,quality=95);files.insert(0,target.name)
    if fmt=='psd':
        from psd_tools import PSDImage
        psd=PSDImage.new('RGB',image.size)
        for layer,name in zip(layers,names):
            created=psd.create_pixel_layer(layer,name='Layer');created.name=name
        psd.save(output/'editable.psd');files.append('editable.psd')
    if fmt=='ora':
        root=ET.Element('image',{'version':'0.0.1','w':str(image.width),'h':str(image.height),'name':'updream image'})
        stack=ET.SubElement(root,'stack')
        with zipfile.ZipFile(output/'editable.ora','w') as z:
            z.writestr('mimetype','image/openraster',compress_type=zipfile.ZIP_STORED)
            for i in reversed(range(len(layers))):ET.SubElement(stack,'layer',{'name':names[i],'src':f'data/{i}.png','opacity':'1.0','visibility':'visible','composite-op':'svg:src-over','x':'0','y':'0'})
            z.writestr('stack.xml',ET.tostring(root,encoding='utf-8',xml_declaration=True))
            for name,im in [('mergedimage.png',image),*[(f'data/{i}.png',l) for i,l in enumerate(layers)]]:
                data=io.BytesIO();im.save(data,format='PNG');z.writestr(name,data.getvalue())
        files.append('editable.ora')
    return files
