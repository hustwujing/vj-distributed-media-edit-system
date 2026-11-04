"""Independent user-review oracle for deterministic feedback transforms."""
import argparse
import hashlib
import json
import math
from pathlib import Path
from PIL import Image,ImageDraw,ImageFilter
import numpy as np


def check(original,candidate,transform):
    patch={}
    with Image.open(original) as source:
        if source.width*source.height>4_000_000:raise ValueError('Preview limit')
        expected=source.convert('RGBA') if transform in ('alpha_erode','alpha_erode_patch') else source.convert('RGB')
    if transform.startswith('border_'):
        width=int(transform.split('_')[1]);ImageDraw.Draw(expected).rectangle((0,0,expected.width-1,expected.height-1),outline='#FF0000',width=width)
    elif transform=='alpha_erode':expected.putalpha(expected.getchannel('A').filter(ImageFilter.MinFilter(3)))
    elif transform=='alpha_erode_patch':
        alpha=expected.getchannel('A');filtered=alpha.filter(ImageFilter.MinFilter(3))
        before=np.asarray(alpha);after=np.asarray(filtered);changed=np.argwhere(before!=after)
        subject=int(np.count_nonzero(before));budget=subject//100
        if not len(changed) or budget<1:raise ValueError('No nonempty local edge patch within the declared budget')
        y,x=map(int,changed[0]);side=min(8,math.isqrt(budget));box=(x,y,min(x+side,expected.width),min(y+side,expected.height))
        alpha.paste(filtered.crop(box),box);expected.putalpha(alpha)
        count=int(np.count_nonzero(before!=np.asarray(alpha)))
        if not 0<count<=budget:raise ValueError('Local patch did not satisfy its nonzero bounded-change contract')
        patch={'patch_box':box,'expected_changed_pixels':count,'reference_subject_pixels':subject,'expected_changed_fraction':count/subject}
    elif transform=='swap_halves':
        width,height=expected.size;copy=expected.copy();expected.paste(copy.crop((width//2,0,width,height)),(0,0));expected.paste(copy.crop((0,0,width//2,height)),(width//2,0))
    elif transform!='unchanged':raise ValueError('Unknown feedback transform')
    with Image.open(candidate) as opened:
        if opened.width*opened.height>4_000_000 or getattr(opened,'n_frames',1)!=1:raise ValueError('Candidate preview limit')
        actual=opened.convert(expected.mode)
    passed=actual.size==expected.size and bool(np.array_equal(np.asarray(actual),np.asarray(expected)))
    return {'passed':passed,'transform':transform,'original_sha256':hashlib.sha256(Path(original).read_bytes()).hexdigest(),
            'candidate_sha256':hashlib.sha256(Path(candidate).read_bytes()).hexdigest(),'expected_size':expected.size,'actual_size':actual.size,
            'method':'exact_decoded_pixels_from_original_candidate',**patch}


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('original',type=Path);parser.add_argument('candidate',type=Path)
    parser.add_argument('transform',choices=['border_2','border_4','alpha_erode','alpha_erode_patch','swap_halves','unchanged']);args=parser.parse_args()
    print(json.dumps(check(args.original,args.candidate,args.transform)))
