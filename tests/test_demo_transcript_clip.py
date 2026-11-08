import copy
import unittest

from webapp.build_demos import cut_transcript
from helpers.timeline_view import find_silences, gap_legend


class TranscriptClipTests(unittest.TestCase):
    def test_clipped_metadata_and_nested_clip_share_word_timebase(self):
        source={'audio_duration_secs':24,'transcription_id':'original', 'words':[
            {'text':'music','type':'audio_event','start':8,'end':10},
            {'text':'hello','type':'word','start':12,'end':13},
            {'text':'outside','type':'word','start':19,'end':20}]}
        before=copy.deepcopy(source)
        clip=cut_transcript(source,9,18)
        self.assertEqual(clip['audio_duration_secs'],9)
        self.assertEqual(clip['source_clip'],{'start':9,'end':18,'audio_duration_secs':24})
        self.assertEqual([(w['start'],w['end']) for w in clip['words']],[(0,1),(3,4)])
        self.assertEqual(clip['transcription_id'],'original')
        nested=cut_transcript(clip,2,5)
        self.assertEqual(nested['audio_duration_secs'],3)
        self.assertEqual(nested['words'],[{'text':'hello','type':'word','start':1,'end':2}])
        self.assertEqual(source,before)

    def test_invalid_clip_is_rejected_before_metadata_is_emitted(self):
        for start,end in ((-1,2),(2,2),(3,2),(0,float('nan')),(0,float('inf'))):
            with self.subTest(start=start,end=end),self.assertRaises(ValueError):
                cut_transcript({'words':[]},start,end)


class TimelineGapEvidenceTests(unittest.TestCase):
    def test_audio_events_cover_speech_pauses_but_zero_is_explicit(self):
        tokens=[{'type':'word','start':0,'end':1},
                {'type':'audio_event','start':1,'end':4},
                {'type':'word','start':4,'end':5}]
        gaps=find_silences(tokens,0,5)
        self.assertEqual(gaps,[])
        self.assertIn('none',gap_legend(True,gaps))
        self.assertIn('audio silence not measured',gap_legend(True,gaps))
        self.assertIn('unavailable',gap_legend(False,[]))

    def test_uncovered_intervals_remain_visible_without_claiming_acoustic_silence(self):
        gaps=find_silences([{'type':'word','start':1,'end':2}],0,3)
        self.assertEqual(gaps,[(0,1),(2,3)])
        self.assertIn('2 gap(s), shaded',gap_legend(True,gaps))
        self.assertIn('audio silence not measured',gap_legend(True,gaps))
