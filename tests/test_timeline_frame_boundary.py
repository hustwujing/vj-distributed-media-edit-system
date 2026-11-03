"""Decode real videos at the exclusive end, including longer audio tails."""
from pathlib import Path
import subprocess
import tempfile
import unittest
from helpers.timeline_view import extract_frames


class TimelineFrameBoundaryTests(unittest.TestCase):
    def verify_end(self,audio_tail=False):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);video=root/'clip.mp4'
            command=['ffmpeg','-v','error','-f','lavfi','-i','testsrc2=size=96x64:rate=30:duration=1']
            if audio_tail:command+=['-f','lavfi','-i','sine=frequency=440:duration=1.2','-c:a','aac']
            command+=['-c:v','libx264','-preset','ultrafast','-threads','1','-y',str(video)]
            subprocess.run(command,check=True,capture_output=True,timeout=20)
            paths=extract_frames(video,0,1.2 if audio_tail else 1,3,root/'frames')
            expected=root/'last.jpg'
            subprocess.run(['ffmpeg','-v','error','-threads','2','-i',str(video),'-frames:v','1',
                '-vf',r'select=eq(n\,29),scale=320:-2','-q:v','4','-threads','1',str(expected)],
                check=True,capture_output=True,timeout=20)
            self.assertEqual(len(paths),3)
            self.assertEqual(paths[-1].read_bytes(),expected.read_bytes(),'Must select the real final frame')

    def test_fractional_last_frame_is_not_rounded_past_eof(self):
        self.verify_end()

    def test_audio_tail_does_not_move_video_seek_past_its_end(self):
        self.verify_end(audio_tail=True)
