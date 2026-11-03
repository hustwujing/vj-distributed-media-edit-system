"""Independent user-review oracle for deterministic feedback transforms."""
import argparse
import hashlib
import json
from pathlib import Path
from PIL import Image,ImageDraw,ImageFilter
import numpy as np


def check(original,candidate,transform):
    with Image.open(original) as source:
        if source.width*source.height>4_000_000:raise ValueError('Preview limit')
        expected=source.convert('RGBA') if transform=='alpha_erode' else source.convert('RGB')
    if transform.startswith('border_'):
        width=int(transform.split('_')[1]);ImageDraw.Draw(expected).rectangle((0,0,expected.width-1,expected.height-1),outline='#FF0000',width=width)
    elif transform=='alpha_erode':expected.putalpha(expected.getchannel('A').filter(ImageFilter.MinFilter(3)))
    elif transform=='swap_halves':
        width,height=expected.size;copy=expected.copy();expected.paste(copy.crop((width//2,0,width,height)),(0,0));expected.paste(copy.crop((0,0,width//2,height)),(width//2,0))
    elif transform!='unchanged':raise ValueError('Unknown feedback transform')
    with Image.open(candidate) as opened:
        if opened.width*opened.height>4_000_000 or getattr(opened,'n_frames',1)!=1:raise ValueError('Candidate preview limit')
        actual=opened.convert(expected.mode)
    passed=actual.size==expected.size and bool(np.array_equal(np.asarray(actual),np.asarray(expected)))
    return {'passed':passed,'transform':transform,'original_sha256':hashlib.sha256(Path(original).read_bytes()).hexdigest(),
            'candidate_sha256':hashlib.sha256(Path(candidate).read_bytes()).hexdigest(),'expected_size':expected.size,'actual_size':actual.size,
            'method':'exact_decoded_pixels_from_original_candidate'}


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('original',type=Path);parser.add_argument('candidate',type=Path)
    parser.add_argument('transform',choices=['border_2','border_4','alpha_erode','swap_halves','unchanged']);args=parser.parse_args()
    print(json.dumps(check(args.original,args.candidate,args.transform)))
