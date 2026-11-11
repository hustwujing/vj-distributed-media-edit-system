"""Publish real outputs for capabilities added after the original demo catalogue."""
from __future__ import annotations
import json,os,pwd,shutil,tempfile
from pathlib import Path
from PIL import Image,ImageDraw,ImageOps

from .paths import data_root
from .image_tasks import execute_image
from .image_document import Document,export_document
from .app import inspect_image
from .quality_review import inspect as quality_review

ROOT=data_root()/'demos';SOURCES=ROOT/'sources';SETUP=data_root()/'setup';REPO=Path(__file__).resolve().parents[1]
CREDIT={'name':'本项目绘制的原创杯子、文档与排版测试素材','url':'/video-use/','license':'原创演示素材，可用于本项目编辑示例'}

def poster(result,destination):
    with Image.open(result) as image:
        image=ImageOps.contain(image.convert('RGBA'),(640,480));canvas=Image.new('RGBA',image.size,'#e8ede3');canvas.alpha_composite(image);canvas.convert('RGB').save(destination,quality=92)

def perspective_document(destination):
    """Warp the whole document so its border and text share one geometry."""
    import cv2
    import numpy as np
    front=Image.new('RGB',(460,300),'#ffffff');draw=ImageDraw.Draw(front)
    draw.rectangle((0,0,459,299),outline='#dfe5e1',width=4)
    draw.text((105,95),'UPDREAM\nDOCUMENT',fill='#1d3429',spacing=20)
    source=np.asarray(front);canvas=np.full((420,640,3),(82,100,93),dtype=np.uint8)
    corners=np.float32([[130,55],[540,95],[500,360],[85,320]])
    matrix=cv2.getPerspectiveTransform(np.float32([[0,0],[459,0],[459,299],[0,299]]),corners)
    warped=cv2.warpPerspective(source,matrix,(640,420),flags=cv2.INTER_LINEAR)
    mask=cv2.warpPerspective(np.full((300,460),255,dtype=np.uint8),matrix,(640,420),flags=cv2.INTER_LINEAR)
    blend=(mask.astype(np.float32)/255.0)[...,None]
    Image.fromarray((warped*blend+canvas*(1-blend)).round().astype(np.uint8)).save(destination)

def image_demo(folder,id,title,category,description,prompt,result,before='image-cup.png',options=(),sources=None):
    target=folder/id;target.mkdir();shutil.copyfile(result,target/'result.png');shutil.copyfile(SOURCES/before,target/'before.png');poster(target/'result.png',target/'poster.jpg')
    return {'id':id,'title':title,'category':category,'description':description,'tags':['图片编辑','真实处理结果'],'media':'image','workflow':'agent','renderer':'image-workbench','prompt':prompt,
      'options':list(options) or [description,'原图保留，结果可载入为新图层'],'sources':sources or [{'key':'main','name':'原创能力演示素材.png','file':before}],
      'preset':{'kind':'agent','assets':[s['key'] for s in (sources or [{'key':'main'}])]},'result':'result.png','result_type':'image/png','poster':'poster.jpg','before':'before.png','before_type':'image/png','files':['result.png','before.png','poster.jpg'],'credits':[CREDIT],'changes':description+'；示例来自当前工作台的实际本地任务或画布渲染。'}

def make_image_examples(staging):
    sample=SETUP/'image-workbench-samples/cup.png';blue=SETUP/'image-workbench-samples/real-local_edit/image.png';source=staging/'project/sources';source.mkdir(parents=True);shutil.copyfile(sample,source/'cup.png');shutil.copyfile(blue,source/'blue.png');shutil.copyfile(SETUP/'image-workbench-samples/point/mask.png',source/'mask.png');shutil.copyfile(SETUP/'image-workbench-samples/foreground.png',source/'foreground.png')
    shutil.copyfile(source/'mask.png',SOURCES/'image-mask.png');shutil.copyfile(source/'blue.png',SOURCES/'image-color-reference.png');shutil.copyfile(source/'foreground.png',SOURCES/'image-foreground.png')
    assets=[{'id':id,'name':name,'filename':name,'metadata':inspect_image(source/name)} for id,name in [('cup','cup.png'),('blue','blue.png'),('mask','mask.png'),('foreground','foreground.png')]]
    outputs=staging/'outputs';outputs.mkdir();items=[]
    # Actual SAM3+ZIM result from the service-user GPU smoke test.
    sam_demo=image_demo(staging,'image-sam3-zim','SAM3 选择主体，ZIM 精修边缘','SAM3＋ZIM 精细蒙版','用文字提示选择杯子，再用 ZIM 生成软 Alpha 边缘。','用文字“coffee cup”让 SAM3 选择杯子，再用 ZIM 精修边缘；先给方案。',SETUP/'sam3-zim-text-smoke/output/foreground.png',options=['SAM3 文字提示','ZIM 软 Alpha','输出透明前景与蒙版'])
    sam_dir=staging/'image-sam3-zim'
    for source_name,target_name in [('sam3-preview.jpg','sam3-preview.jpg'),('zim-preview.jpg','zim-preview.jpg'),('mask.png','mask.png'),('sam3-zim.json','sam3-zim.json')]:shutil.copyfile(SETUP/'sam3-zim-text-smoke/output'/source_name,sam_dir/target_name)
    sam_demo['files']+=['sam3-preview.jpg','zim-preview.jpg','mask.png','sam3-zim.json'];sam_demo['detail']={'stages':[{'label':'原图','file':'before.png'},{'label':'SAM3','file':'sam3-preview.jpg'},{'label':'ZIM','file':'zim-preview.jpg'},{'label':'透明前景','file':'result.png'},{'label':'最终蒙版','file':'mask.png'}]};items.append(sam_demo)
    tasks=[
      ('image-mask-refine','清理蒙版边缘与孔洞','蒙版精修','扩张、羽化、去小区域并填补孔洞。','精修这张蒙版：扩张2像素、羽化2像素、填补孔洞。','refine_mask','mask',{'grow':2,'feather':2,'threshold':128,'remove_small':20,'fill_holes':True},None),
      ('image-auto-adjust','自动白平衡、色阶与曲线','图片增强','本地调整白平衡、色阶、Gamma、饱和度与锐化。','自动修正白平衡和色阶，Gamma设为1.1，轻微锐化。','local_adjust','cup',{'auto_white_balance':True,'auto_levels':True,'gamma':1.1,'sharpen':.4},None),
      ('image-color-match','参考另一张图统一色彩','色彩匹配','按参考图的通道统计匹配色彩，可调节强度。','让原图按参考图匹配色彩，强度0.7。','color_match','cup',{'strength':.7},['blue']),
      ('image-product-layout','统一商品留白与自然投影','商品图生产','透明主体居中、统一留白、白底和自然投影。','把透明杯子放进1200×1200白底商品画布，统一留白并添加投影。','product_layout','foreground',{'width':600,'height':600,'background':'#ffffff','margin':70,'shadow':True,'shadow_blur':18},None),
      ('image-watermark-batch','统一尺寸、格式与水印','图片批处理','批量导出统一尺寸、背景、格式和文字水印。','把项目图片批量导出为600×600并添加“UPDREAM”水印。','batch_export','cup',{'width':600,'height':600,'fit':'contain','background':'#ffffff','format':'png','quality':90,'watermark':'UPDREAM'},None)]
    for id,title,cat,desc,prompt,action,asset,params,refs in tasks:
        out=outputs/id;files=execute_image({'action':action,'asset':asset,'references':refs or [],'parameters':params},assets,staging/'project',out)
        name=next(n for n in files if Path(n).suffix.lower() in ('.png','.jpg','.webp'))
        if id=='image-mask-refine':demo=image_demo(staging,id,title,cat,desc,prompt,out/name,'image-mask.png',sources=[{'key':'main','name':'待精修蒙版.png','file':'image-mask.png'}])
        elif id=='image-color-match':
            demo=image_demo(staging,id,title,cat,desc,prompt,out/name,sources=[{'key':'main','name':'待匹配图片.png','file':'image-cup.png'},{'key':'reference','name':'色彩参考图.png','file':'image-color-reference.png'}])
            shutil.copyfile(SOURCES/'image-color-reference.png',staging/id/'reference.png');demo['files'].append('reference.png')
        elif id=='image-product-layout':demo=image_demo(staging,id,title,cat,desc,prompt,out/name,'image-foreground.png',sources=[{'key':'main','name':'透明商品主体.png','file':'image-foreground.png'}])
        else:demo=image_demo(staging,id,title,cat,desc,prompt,out/name)
        items.append(demo)
    ocr_data=json.loads((SETUP/'image-workbench-samples/ocr/result.json').read_text());ocr_preview=Image.open(sample).convert('RGB');ocr_draw=ImageDraw.Draw(ocr_preview)
    for line in ocr_data['lines']:
        x,y,w,h=line['box'];ocr_draw.rectangle((x,y,x+w,y+h),outline='#45b977',width=3);ocr_draw.text((x,y+h+3),f"{line['confidence']:.0%}",fill='#1d6b44')
    ocr_preview.save(outputs/'ocr-preview.png');ocr_demo=image_demo(staging,'image-ocr','识别图片文字和位置','OCR 文字识别','识别中文内容、置信度和文字框，不修改原图。','识别图片中的文字，展示文字框和置信度。',outputs/'ocr-preview.png');shutil.copyfile(SETUP/'image-workbench-samples/ocr/result.json',staging/'image-ocr/ocr.json');ocr_demo['files'].append('ocr.json');items.append(ocr_demo)
    out=outputs/'replace-text';files=execute_image({'action':'replace_text','asset':'cup','box':ocr_data['lines'][0]['box'],'text':'夏日新品','size':24,'color':'#202020'},assets,staging/'project',out);items.append(image_demo(staging,'image-replace-text','擦除原字并替换为新文字','选区改字','识别文字框后修补背景并栅格化新中文字。','把“春季新品”改成“夏日新品”，保留杯子和背景。',out/'image.png'))
    # A visibly skewed synthetic document and its corrected result.
    perspective_document(SOURCES/'image-document-skew.png')
    shutil.copyfile(SOURCES/'image-document-skew.png',source/'document.png');doc_assets=[{'id':'doc','name':'document.png','filename':'document.png','metadata':inspect_image(source/'document.png')}];out=outputs/'perspective';files=execute_image({'action':'perspective','asset':'doc','points':[[130,55],[540,95],[500,360],[85,320]]},doc_assets,staging/'project',out)
    items.append(image_demo(staging,'image-perspective','四点校正文档透视','透视与扫描','按四个角点拉正倾斜文档。','依次选择文档四角，校正透视并导出。',out/'image.png','image-document-skew.png'))
    # Canvas-only examples exercise versioned layers and exact export rendering.
    canvas_assets={a['id']:a for a in assets}
    docs=[('image-adjustment-layers','用调整图层控制整体色调','调整图层与混合模式','调整图层保留参数，可随时修改或关闭。','添加调整图层，提高对比度、降低饱和度并使用暖色温。',
       {'width':384,'height':256,'background':'transparent','layers':[{'id':'base','kind':'image','asset':'cup','name':'原图','width':384,'height':256},{'id':'adjust','kind':'adjustment','name':'暖调调整','contrast':1.18,'saturation':.72,'temperature':.25,'gamma':1.08,'opacity':.9}]}),
      ('image-text-effects','自动换行、描边与阴影','文字排版','文字保持可编辑并自动换行，支持描边、阴影和居中。','添加居中的中文标题，自动换行，使用白色文字、黑色描边和阴影。',
       {'width':384,'height':256,'background':'transparent','layers':[{'id':'base','kind':'image','asset':'cup','name':'原图','width':384,'height':256},{'id':'title','kind':'text','name':'标题','text':'自动换行标题示例','x':30,'y':40,'width':324,'height':160,'size':52,'color':'#ffffff','stroke_color':'#000000','stroke_width':3,'shadow_color':'#000000','shadow_blur':4,'shadow_x':3,'shadow_y':4,'text_align':'center'}]}),
      ('image-layout-guides','对齐、吸附与等距排版','画布排版','多个图层按画布对齐并保持等距。','把三张图片在画布中水平等距排列并居中对齐。',
       {'width':720,'height':300,'background':'#f1f4ed','layers':[{'id':f'cup{i}','kind':'image','asset':'cup','name':f'杯子{i}','x':40+i*230,'y':70,'width':180,'height':120} for i in range(3)]})]
    for id,title,cat,desc,prompt,raw in docs:
        out=outputs/id;out.mkdir();export_document(Document.model_validate(raw),canvas_assets,staging/'project',out,'png');items.append(image_demo(staging,id,title,cat,desc,prompt,out/'image.png'))
    return items

def clone_video_demo(staging,catalog,id,title,category,description,prompt,source_id,result_name=None):
    source=next(d for d in catalog if d['id']==source_id);target=staging/id;target.mkdir();result_name=result_name or source['result'];shutil.copyfile(ROOT/source_id/result_name,target/result_name);shutil.copyfile(ROOT/source_id/source['before'],target/'before.mp4');shutil.copyfile(ROOT/source_id/source['poster'],target/'poster.jpg')
    files=[result_name,'before.mp4','poster.jpg'];return {'id':id,'title':title,'category':category,'description':description,'tags':['编辑工作流','可继续对话'],'workflow':'agent','prompt':prompt,'options':[description,'可载入项目继续编辑'],'sources':source['sources'],'preset':{'kind':'agent','assets':[s['key'] for s in source['sources']]},'result':result_name,'result_type':source['result_type'],'poster':'poster.jpg','before':'before.mp4','files':files,'credits':source['credits'],'changes':description+'；复用当前系统已经验证的真实成片或报告作为工作流入口。'}

def main():
    catalog=json.loads((ROOT/'catalog.json').read_text());ids={
      'image-sam3-zim','image-mask-refine','image-auto-adjust','image-color-match','image-product-layout','image-watermark-batch','image-perspective','image-adjustment-layers','image-text-effects','image-layout-guides','image-ocr','image-replace-text',
      'video-quality-review','video-version-compare','video-caption-refine','video-chapter-shorts','video-brand-batch','video-shot-notes'}
    with tempfile.TemporaryDirectory(prefix='missing-demo-',dir=data_root()/'tmp') as temporary:
        staging=Path(temporary);images=make_image_examples(staging)
        videos=[
          clone_video_demo(staging,catalog,'video-version-compare','同步比较两个视频版本','视频版本对比','左右同步播放并选择要继续使用的版本。','比较当前两个视频版本，指出差异并建议保留哪一个。','grade'),
          clone_video_demo(staging,catalog,'video-caption-refine','逐句修正字幕时间与文本','字幕精修','逐句编辑字幕并另存为新 SRT，不覆盖转写。','载入现有字幕，逐句检查文本和时间，再生成新字幕版本。','subtitles'),
          clone_video_demo(staging,catalog,'video-chapter-shorts','从章节生成独立短片','章节与短片拆条','保存章节范围并作为独立短片进入 Agent。','把这个章节拆成一条独立短片，先给我节奏和字幕方案。','local-word-edit'),
          clone_video_demo(staging,catalog,'video-brand-batch','按品牌规则批量输出视频','品牌模板与批处理','统一画幅、字幕样式和响度并批量排队。','按同一个品牌模板处理这些视频，统一画幅、字幕和响度。','agent-portrait'),
          clone_video_demo(staging,catalog,'video-shot-notes','按时间点批注并局部返工','镜头批注返工','在具体时间段记录修改要求，交给 Agent 生成返工方案。','根据时间线批注只修改对应镜头，保留其他成片内容。','cuts')]
        # Quality review is a real deterministic report.
        source=next(d for d in catalog if d['id']=='cuts');target=staging/'video-quality-review';target.mkdir();video=SOURCES/source['sources'][0]['file'];metadata={'kind':'video',**__import__('webapp.app',fromlist=['inspect_media']).inspect_media(video)};report=quality_review(video,metadata);(target/'quality-review.json').write_text(json.dumps(report,ensure_ascii=False,indent=2));shutil.copyfile(video,target/'before.mp4');shutil.copyfile(ROOT/'cuts'/'poster.jpg',target/'poster.jpg')
        quality={'id':'video-quality-review','title':'检查黑帧、静音和轨道结构','category':'成片质检','description':'不使用模型，输出可复核的确定性质检信号。','tags':['确定性质检','无需模型'],'workflow':'agent','prompt':'检查这条视频的黑帧、静帧、静音、时长和轨道结构，并给出复核位置。','options':['黑帧、静帧、静音','时长与音视频轨','输出 JSON 报告'],'sources':source['sources'],'preset':{'kind':'agent','assets':[s['key'] for s in source['sources']]},'result':'quality-review.json','result_type':'application/json','poster':'poster.jpg','before':'before.mp4','files':['quality-review.json','before.mp4','poster.jpg'],'credits':source['credits'],'changes':'使用当前 FFmpeg 确定性质检器实际生成。'}
        additions=[*images,*videos,quality];catalog=[d for d in catalog if d['id'] not in ids]
        for demo in additions:
            src=staging/demo['id'];dst=ROOT/demo['id'];dst.mkdir(exist_ok=True)
            for file in src.iterdir():temp=dst/(file.name+'.new');shutil.copyfile(file,temp);temp.replace(dst/file.name)
            catalog.append(demo)
        layered=next((d for d in catalog if d['id']=='image-layers'),None)
        if layered:
            layer_dir=ROOT/'image-layers'
            for name in ('layer-01.png','layer-02.png','layer-03.png','editable-layers.psd','editable-layers.ora','layer-report.json'):
                shutil.copyfile(SETUP/'image-workbench-samples/real-decompose'/name,layer_dir/name)
                if name not in layered['files']:layered['files'].append(name)
        from .demo_agent_requirements import apply as apply_agent_requirements
        catalog,_=apply_agent_requirements(catalog)
        temp=ROOT/'catalog.missing-new.json';temp.write_text(json.dumps(catalog,ensure_ascii=False,indent=2));temp.replace(ROOT/'catalog.json')
        if os.geteuid()==0:
            user=pwd.getpwnam('video-use')
            for path in [ROOT/'catalog.json',SOURCES/'image-document-skew.png',SOURCES/'image-mask.png',SOURCES/'image-color-reference.png',SOURCES/'image-foreground.png',*[p for d in additions for p in (ROOT/d['id'],*(ROOT/d['id']).iterdir())]]:os.chown(path,user.pw_uid,user.pw_gid);path.chmod(0o750 if path.is_dir() else 0o640)
    print(f'Published {len(additions)} missing capability examples; total {len(catalog)}')

if __name__=='__main__':main()
