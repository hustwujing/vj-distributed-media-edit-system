import argparse
import contextlib
import io
import os
import sys
import tempfile
import unittest
from pathlib import Path
from tests.isolation import temporary_root as isolated_tmp_root

from PIL import Image
from webapp.preview_export import inspect_previews

sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'helpers'))
from preview_style import create_preview
from edit_plan import PlanError


class StylePreviewTests(unittest.TestCase):
    def test_image_is_drawn_without_video_and_output_is_scoped(self):
        with tempfile.TemporaryDirectory(dir=isolated_tmp_root()) as directory:
            root=Path(directory);out=root/'edit/previews/turn/style.png'
            args=argparse.Namespace(workspace=root,output=out,width=640,height=360,input=None,time=0,
                text='寻找旅伴',font_size=36,color='#FFE066',position='bottom',no_box=False,fit='contain')
            result=create_preview(args)
            self.assertTrue(result['preview_only'])
            with Image.open(out) as image:
                self.assertEqual(image.size,(640,360))
                self.assertGreater(len(image.getcolors(640*360)),10)
            args.output=root/'edit/final.png'
            with self.assertRaises(PlanError):create_preview(args)

    def test_export_inspection_rejects_invalid_formats_links_animation_and_pipes(self):
        with tempfile.TemporaryDirectory(dir=isolated_tmp_root()) as directory:
            root=Path(directory)
            Image.new('RGB',(64,64),'red').save(root/'valid.png')
            (root/'wrong.png').write_text('<svg/>')
            (root/'not-image.webp').write_text('text')
            Image.new('RGB',(64,64),'red').save(root/'disguised.jpg',format='PNG')
            Image.new('RGB',(64,64),'red').save(root/'animation.png',save_all=True,
                append_images=[Image.new('RGB',(64,64),'blue')],duration=100,loop=0)
            (root/'link.png').symlink_to(root/'valid.png')
            os.mkfifo(root/'fifo.png')
            (root/'video.mp4').write_bytes(b'video')
            records=inspect_previews(root)
            self.assertEqual([r['name'] for r in records],['valid.png'])


if __name__=='__main__':unittest.main()
