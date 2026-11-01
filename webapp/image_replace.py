"""Fixed OCR-region replacement. Never evaluates user code."""
import json,sys
import cv2
import numpy as np
from PIL import Image,ImageDraw,ImageFont
from pathlib import Path
r=json.loads(Path(sys.argv[1]).read_text())
image=Image.open(r['source']).convert('RGBA');mask=Image.open(r['mask']).convert('L');box=mask.getbbox()
if not box:raise ValueError('蒙版为空')
clean=cv2.inpaint(np.asarray(image.convert('RGB')),np.asarray(mask),3,cv2.INPAINT_TELEA)
canvas=Image.fromarray(clean).convert('RGBA');canvas.putalpha(image.getchannel('A'))
draw=ImageDraw.Draw(canvas);size=r['size'];font=ImageFont.truetype(r['font'],size)
x,y,right,bottom=box
while draw.textbbox((0,0),r['text'],font=font)[2]>right-x and size>8:size-=1;font=ImageFont.truetype(r['font'],size)
layer=Image.new('RGBA',image.size);ImageDraw.Draw(layer).text((x,y),r['text'],font=font,fill=r['color'],anchor='lt')
canvas=Image.alpha_composite(canvas,layer)
Image.composite(canvas,image,mask).save(r['output'])
