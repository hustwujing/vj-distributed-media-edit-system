"""Dense/short transcript words remain visible in a bounded real PNG layout."""
import tempfile,unittest
from pathlib import Path
from unittest.mock import patch
from PIL import Image,ImageDraw
from helpers import timeline_view as timeline


class TimelineWordLabelsTests(unittest.TestCase):
    def words(self):
        return [{'type':'word','text':text,'start':.4+i*.02,'end':.41+i*.02}
                for i,text in enumerate(['I','am','in','the','a','to','longer-word'])]

    def test_short_adjacent_words_are_all_retained_without_overlap(self):
        words=self.words();labels,lanes=timeline.layout_word_labels(words,lambda t:int(t*100),0,300,timeline.load_font(12))
        self.assertEqual([x['text'] for x in labels],[w['text'] for w in words])
        self.assertGreater(lanes,1)
        for label in labels:
            self.assertGreaterEqual(label['x'],0);self.assertLessEqual(label['x']+label['width'],300)
        for lane in range(lanes):
            row=sorted((x for x in labels if x['lane']==lane),key=lambda x:x['x'])
            for a,b in zip(row,row[1:]):self.assertLess(a['x']+a['width'],b['x'])

    def test_real_png_render_draws_every_word_and_expands_annotation_space(self):
        import json,numpy as np
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);frame=root/'frame.png';Image.new('RGB',(160,90),'gray').save(frame)
            transcript=root/'words.json';transcript.write_text(json.dumps({'words':self.words()}))
            drawn=[];original=ImageDraw.ImageDraw.text
            def capture(draw,xy,text,*args,**kwargs):
                drawn.append((xy,text));return original(draw,xy,text,*args,**kwargs)
            with patch.object(timeline,'extract_frames',return_value=[frame,frame]), \
                 patch.object(timeline,'compute_envelope',return_value=np.zeros(200)), \
                 patch.object(ImageDraw.ImageDraw,'text',new=capture):
                timeline.render_timeline(root/'input.mp4',0,8,root/'timeline.png',2,transcript)
            labels=[(xy,text) for xy,text in drawn if text in {w['text'] for w in self.words()}]
            self.assertEqual(len(labels),len(self.words()))
            with Image.open(root/'timeline.png') as result:
                self.assertGreater(result.height,540)
                self.assertTrue(all(230<=xy[1]<result.height-250 for xy,text in labels))

    def test_excess_density_fails_explicitly_instead_of_dropping_words(self):
        words=[{'type':'word','text':'word','start':0,'end':.01} for _ in range(65)]
        with self.assertRaisesRegex(ValueError,'too dense'):
            timeline.layout_word_labels(words,lambda t:0,0,100,timeline.load_font(12))
