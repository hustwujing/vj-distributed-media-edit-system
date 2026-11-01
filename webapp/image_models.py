"""Fixed local model worker; never accepts model paths, Python or workflow code."""
import argparse
import json
import os
import sys
from pathlib import Path


def main():
    parser=argparse.ArgumentParser();parser.add_argument('action',choices=['segment','remove_bg','ocr']);parser.add_argument('request');a=parser.parse_args()
    req=json.loads(Path(a.request).read_text());source=Path(req['source']);out=Path(req['output'])
    from PIL import Image,ImageOps
    import numpy as np
    image=ImageOps.exif_transpose(Image.open(source)).convert('RGB')
    if a.action=='ocr':
        from paddleocr import PaddleOCR
        root=Path('/data/wujing03/models/media-foundation/paddleocr/.paddleocr/whl')
        ocr=PaddleOCR(use_gpu=False,use_angle_cls=False,show_log=False,lang='ch',
            det_model_dir=str(root/'det/ch/ch_PP-OCRv4_det_infer'),rec_model_dir=str(root/'rec/ch/ch_PP-OCRv4_rec_infer'),
            cls_model_dir=str(root/'cls/ch_ppocr_mobile_v2.0_cls_infer'))
        lines=ocr.ocr(np.asarray(image)[:,:,::-1],cls=False);result=[]
        for box,(text,score) in (lines[0] if lines else []) or []:
            if score<.4:continue
            xs,ys=zip(*box);result.append({'text':text,'confidence':float(score),'box':[max(0,int(min(xs))),max(0,int(min(ys))),int(max(xs)-min(xs)),int(max(ys)-min(ys))]})
        out.write_text(json.dumps({'width':image.width,'height':image.height,'lines':result},ensure_ascii=False));return
    import torch
    torch.set_num_threads(4)
    if a.action=='segment':
        from transformers import Sam2Model,Sam2Processor
        root='/data/wujing03/models/media-foundation/sam2.1-hiera-large'
        processor=Sam2Processor.from_pretrained(root,local_files_only=True)
        model=Sam2Model.from_pretrained(root,local_files_only=True).to('cuda').eval()
        if req.get('box'):
            x,y,w,h=req['box'];inputs=processor(images=image,input_boxes=[[[x,y,x+w,y+h]]],return_tensors='pt').to('cuda')
        else:inputs=processor(images=image,input_points=[[req['points']]],input_labels=[[[1 for _ in req['points']]]],return_tensors='pt').to('cuda')
        with torch.inference_mode():prediction=model(**inputs)
        masks=processor.post_process_masks(prediction.pred_masks.cpu(),inputs['original_sizes'].cpu())[0]
        best=int(prediction.iou_scores.flatten().argmax());mask=masks.reshape(-1,*image.size[::-1])[best].numpy().astype('uint8')*255
    else:
        sys.path.insert(0,'/data/wujing03/wan22-animate/ComfyUI')
        # Comfy parses process argv at import; isolate its flags from our CLI.
        sys.argv=[sys.argv[0]]
        from comfy.bg_removal_model import load
        model=load('/data/wujing03/image-relighting/models/background_removal/birefnet.safetensors')
        with torch.inference_mode():mask=model.encode_image(torch.from_numpy(np.asarray(image).copy()).float()[None]/255)[0].cpu().numpy()
        mask=np.clip(mask*255,0,255).astype('uint8')
    Image.fromarray(mask).save(out)

if __name__=='__main__':main()
