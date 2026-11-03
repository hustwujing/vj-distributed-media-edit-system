"""Deterministic helper validation should reach diagnosis without identical retries."""
import io
import json
import sys
import subprocess
import tempfile
import unittest
from contextlib import redirect_stdout, redirect_stderr
from pathlib import Path
from unittest.mock import patch
from tests import test_workflows as fixtures
from webapp.execution_contracts import classify_error
from webapp.workflow_runtime import StepFailure

class StructuredStepErrorTests(unittest.TestCase):
    def test_only_trusted_before_request_failure_is_known_not_submitted(self):
        self.assertEqual(classify_error({'code':'pre_submit','request_sent':False},'submit'),'pre_submit')
        for detail in ({'code':'pre_submit'},{'code':'pre_submit','request_sent':True},
                       {'log_tail':json.dumps({'code':'pre_submit','request_sent':False})}):
            self.assertEqual(classify_error(detail,'submit'),'submission_unknown')
    def test_helper_exposes_parameter_error_and_runner_diagnoses_once(self):
        sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'helpers'))
        import edit_media
        output=io.StringIO()
        with redirect_stdout(output),redirect_stderr(io.StringIO()):code=edit_media.main(['validate'])
        self.assertEqual(code,1)
        record=json.loads(output.getvalue());self.assertEqual(record['code'],'parameters')
        f=fixtures.WorkflowTests();f.setUp()
        try:
            step=fixtures.local_step();step['on_failure']='diagnose';f.submit([step])
            with patch.object(f.adapter,'local',side_effect=StepFailure({'message':'Local command failed','log_tail':output.getvalue()+'CalledProcessError: exit 1'})) as call:
                f.tick();self.assertEqual(call.call_count,1)
            w=f.get();self.assertEqual(w['steps'][0]['attempts'],1)
            self.assertEqual(w['agent_review']['purpose'],'diagnose')
        finally:f.tearDown()
    def test_real_helper_failure_survives_captured_stderr_wrapper(self):
        root=Path(__file__).resolve().parents[1]
        with tempfile.TemporaryDirectory() as directory:
            invalid=Path(directory)/'invalid.json';invalid.write_text('{}')
            for helper,args in [('edit_media.py',['validate']),('image_edit.py',['validate',str(invalid),'--workspace',directory])]:
                with self.subTest(helper=helper):
                    command=[sys.executable,str(root/'helpers'/helper),*args]
                    child=subprocess.run(command,capture_output=True,text=True)
                    self.assertEqual(child.returncode,1)
                    self.assertEqual(json.loads(child.stdout),json.loads(child.stderr))
                    wrapper=subprocess.run([sys.executable,'-c',
                        'import subprocess,sys; p=subprocess.run(sys.argv[1:],capture_output=True,text=True);\n'
                        'if p.returncode: raise RuntimeError(p.stderr[-8000:])',*command],capture_output=True,text=True)
                    self.assertEqual(wrapper.returncode,1)
                    detail={'message':'本地处理命令执行失败','log_tail':wrapper.stderr}
                    self.assertEqual(classify_error(detail,'local'),'parameters')
                    self.assertEqual(classify_error(detail,'submit'),'submission_unknown')
                    f=fixtures.WorkflowTests();f.setUp()
                    try:
                        step=fixtures.local_step();step['on_failure']='diagnose';f.submit([step])
                        with patch.object(f.adapter,'local',side_effect=StepFailure(detail)) as call:
                            f.tick();self.assertEqual(call.call_count,1)
                        self.assertEqual(f.get()['agent_review']['purpose'],'diagnose')
                    finally:f.tearDown()
    def test_structured_output_does_not_reclassify_external_submission_or_transient_timeout(self):
        line=json.dumps({'stage':'error','code':'parameters','error':'invalid range'})
        self.assertEqual(classify_error({'log_tail':line},'submit'),'submission_unknown')
        self.assertEqual(classify_error({'log_tail':'TimeoutError: timed out'},'local'),'timeout')
        self.assertEqual(classify_error({'log_tail':'not JSON'},'local'),'execution')
    def test_deterministic_sandbox_and_assertion_failures_do_not_repeat(self):
        for trace,expected in [('OSError: [Errno 30] Read-only file system: /workspace/edit/final.mp4','environment'),
                               ('AssertionError: expected opaque foreground','code')]:
            with self.subTest(expected=expected):
                detail={'log_tail':trace}
                self.assertEqual(classify_error(detail,'local'),expected)
                self.assertEqual(classify_error(detail,'submit'),'submission_unknown')
                f=fixtures.WorkflowTests();f.setUp()
                try:
                    step=fixtures.local_step();step['on_failure']='diagnose';f.submit([step])
                    with patch.object(f.adapter,'local',side_effect=StepFailure(detail)) as call:
                        f.tick();self.assertEqual(call.call_count,1)
                    self.assertEqual(f.get()['agent_review']['purpose'],'diagnose')
                finally:f.tearDown()

    def test_real_output_contract_diagnostics_distinguish_missing_empty_and_invalid_files(self):
        root=Path(__file__).resolve().parents[1]
        cases={'missing':'pass','empty':"p.touch()",'not_file':"p.mkdir()",'symlink':"p.symlink_to('absent')"}
        for problem,statement in cases.items():
            with self.subTest(problem=problem),tempfile.TemporaryDirectory() as directory:
                command=[sys.executable,'-c',"import os;from pathlib import Path;p=Path(os.environ['VIDEO_USE_STEP_OUTPUT'])/'receipt.dat';"+statement]
                request={'mode':'local','output':'edit/workflows/diagnostic','argv':command,'outputs':['receipt.dat']}
                result=subprocess.run([sys.executable,str(root/'webapp/workflow_io.py')],input=json.dumps(request),cwd=directory,capture_output=True,text=True)
                self.assertEqual(result.returncode,1);detail=json.loads(result.stdout)
                self.assertEqual(detail['code'],'output_contract');self.assertEqual(detail['output']['problem'],problem)
                self.assertEqual(detail['output']['bytes'],0 if problem=='empty' else None)
                self.assertEqual(detail['output']['name'],'receipt.dat')
                # Classification depends on protocol data, not this wording.
                detail['message']='arbitrary unrelated wording'
                self.assertEqual(classify_error(detail,'local'),'code')
                self.assertEqual(classify_error(detail,'submit'),'submission_unknown')
                f=fixtures.WorkflowTests();f.setUp()
                try:
                    step=fixtures.local_step();step['on_failure']='diagnose';f.submit([step])
                    with patch.object(f.adapter,'local',side_effect=StepFailure(detail)) as call:
                        f.tick();self.assertEqual(call.call_count,1)
                    self.assertEqual(f.get()['steps'][0]['attempts'],1)
                    self.assertEqual(f.get()['agent_review']['purpose'],'diagnose')
                finally:f.tearDown()

    def test_nonempty_status_report_preserves_success_and_optional_empty_raw_log(self):
        root=Path(__file__).resolve().parents[1]
        with tempfile.TemporaryDirectory() as directory:
            script="import os,json;from pathlib import Path;p=Path(os.environ['VIDEO_USE_STEP_OUTPUT']);(p/'raw.log').touch();(p/'status.json').write_text(json.dumps({'exit_code':0,'stderr_bytes':0}))"
            request={'mode':'local','output':'edit/workflows/status','argv':[sys.executable,'-c',script],'outputs':['status.json']}
            result=subprocess.run([sys.executable,str(root/'webapp/workflow_io.py')],input=json.dumps(request),cwd=directory,capture_output=True,text=True)
            self.assertEqual(result.returncode,0,result.stdout+result.stderr)
            self.assertEqual(json.loads((Path(directory)/request['output']/'status.json').read_text()),{'exit_code':0,'stderr_bytes':0})
