import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from webapp.sam3_zim import run_sam3


class Sam3ZimTests(unittest.TestCase):
    def test_text_selection_retries_once_at_a_lower_confidence(self):
        with tempfile.TemporaryDirectory() as directory:
            work=Path(directory);source=work/'source.png';source.write_bytes(b'image')
            thresholds=[]
            def request(url,payload,timeout=300):
                if url.endswith('/shutdown'):return {}
                thresholds.append(payload['confidence']);output=work/'sam3';detections=output/f'detections-{len(thresholds)}.json'
                records=[]
                if len(thresholds)==2:
                    (output/'mask.png').write_bytes(b'mask');(output/'preview.jpg').write_bytes(b'preview')
                    records=[{'score':.2,'mask_pixels':100,'mask':'mask.png','preview':'preview.jpg'}]
                detections.write_text(json.dumps({'detections':records}));return {'detections':str(detections)}
            process=SimpleNamespace(poll=lambda:None,wait=lambda timeout=None:0,kill=lambda:None)
            with patch('webapp.sam3_zim.subprocess.Popen',return_value=process), \
                 patch('webapp.sam3_zim.wait_health'),patch('webapp.sam3_zim.request_json',side_effect=request):
                run_sam3({'source':str(source),'prompt':'specific subject','confidence':.4},work)
            self.assertEqual(thresholds,[.4,.2])
            self.assertTrue((work/'raw-mask.png').is_file())

    def test_text_selection_does_not_retry_below_point_one(self):
        with tempfile.TemporaryDirectory() as directory:
            work=Path(directory);source=work/'source.png';source.write_bytes(b'image');thresholds=[]
            def request(url,payload,timeout=300):
                if url.endswith('/shutdown'):return {}
                thresholds.append(payload['confidence']);detections=work/'sam3'/'detections.json'
                detections.write_text(json.dumps({'detections':[]}));return {'detections':str(detections)}
            process=SimpleNamespace(poll=lambda:None,wait=lambda timeout=None:0,kill=lambda:None)
            from webapp.sam3_zim import NoSelectionError
            with patch('webapp.sam3_zim.subprocess.Popen',return_value=process), \
                 patch('webapp.sam3_zim.wait_health'),patch('webapp.sam3_zim.request_json',side_effect=request), \
                 self.assertRaises(NoSelectionError):
                run_sam3({'source':str(source),'prompt':'specific subject','confidence':.1},work)
            self.assertEqual(thresholds,[.1])
