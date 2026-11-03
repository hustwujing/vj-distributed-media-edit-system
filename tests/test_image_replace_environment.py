import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from PIL import Image
from webapp.image_tasks import execute_image

@unittest.skipUnless(Path('/data/wujing03/miniforge3/envs/reference_video_remix_ocr/bin/python').is_file(),'Fixed OpenCV interpreter not installed')
class ImageReplaceEnvironmentTests(unittest.TestCase):
 def test_foreign_interpreter_ignores_parent_python_home_and_preserves_outside_pixels(self):
  with tempfile.TemporaryDirectory() as directory:
   project=Path(directory);(project/'sources').mkdir();source=project/'sources/source.png'
   Image.new('RGB',(64,64),'white').save(source)
   assets=[{'id':'source','filename':'source.png','metadata':{'kind':'image','width':64,'height':64}}]
   with patch.dict(os.environ,{'PYTHONHOME':'/not-the-foreign-interpreter','PYTHONPATH':'/not-its-packages'}):
    files=execute_image({'action':'replace_text','asset':'source','box':[8,8,40,24],'text':'30','size':16,'color':'#000000'},assets,project,project/'out')
   self.assertIn('image.png',files)
   with Image.open(project/'out/image.png') as result:
    self.assertEqual(result.size,(64,64));self.assertEqual(result.convert('RGB').getpixel((0,0)),(255,255,255))
    self.assertLess(min(result.convert('RGB').crop((8,8,48,32)).getdata())[0],255)
