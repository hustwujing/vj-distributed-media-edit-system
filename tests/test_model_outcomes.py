"""Empty inference is a model outcome, not a transient execution failure."""
import contextlib
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch

from webapp import sam3_zim
from webapp.image_providers import ModelResultError, check_model_outcome, model_image
from webapp.telemetry import write_child_error
from webapp.execution_contracts import classify_error


class ModelOutcomeTests(unittest.TestCase):
    def test_sam3_empty_detection_is_typed_and_worker_is_stopped(self):
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder);detections=root/'detections.json'
            detections.write_text(json.dumps({'detections':[]}))
            process=Mock()
            with patch.object(sam3_zim.subprocess,'Popen',return_value=process), \
                 patch.object(sam3_zim,'wait_health'), \
                 patch.object(sam3_zim,'request_json',return_value={'detections':str(detections)}) as request:
                with self.assertRaises(sam3_zim.NoSelectionError):
                    sam3_zim.run_sam3({'source':'input.png','prompt':'arbitrary object'},root)
            self.assertTrue(request.call_args.args[0].endswith('/shutdown'))
            process.wait.assert_called_once()
            self.assertFalse((root/'raw-mask.png').exists())

    def test_real_child_outcome_survives_verbose_logs_and_retains_classification(self):
        real_popen=subprocess.Popen
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder)
            def child(*args,**kwargs):
                code="from pathlib import Path; import json; print('x'*10000); Path('model-outcome.json').write_text(json.dumps({'schema':1,'code':'no_selection'})); raise SystemExit(2)"
                return real_popen([sys.executable,'-c',code],**kwargs)
            with patch.dict(os.environ,{'VIDEO_USE_GPU_ASSIGNED':'1'}),patch('webapp.image_providers.subprocess.Popen',side_effect=child):
                with self.assertRaises(ModelResultError) as error:
                    model_image('segment_sam3',root/'input.png',root/'output.png',{},root)
            log=io.StringIO()
            with contextlib.redirect_stderr(log):write_child_error(error.exception)
            self.assertEqual(classify_error({'log_tail':log.getvalue()},'local'),'model_result')
            self.assertEqual(classify_error({'log_tail':log.getvalue()},'submit'),'submission_unknown')

    def test_unknown_malformed_or_symlink_outcome_does_not_change_failure_type(self):
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder);outcome=root/'model-outcome.json'
            for content in ('invalid','{}',json.dumps({'schema':1,'code':'unknown'}),'x'*5000):
                outcome.write_text(content);check_model_outcome(root)
            target=root/'private.json';target.write_text(json.dumps({'schema':1,'code':'no_selection'}))
            outcome.unlink();outcome.symlink_to(target);check_model_outcome(root)
