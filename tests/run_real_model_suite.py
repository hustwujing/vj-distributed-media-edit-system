import json
import os
from pathlib import Path
import unittest
from tests.run_workflow_suite import source_digest


def main():
    os.environ['VIDEO_USE_TEST_REAL_MODELS']='1'
    before=source_digest()
    suite=unittest.defaultTestLoader.loadTestsFromNames([
        'tests.test_execution_runtime.RuntimeTests.test_real_birefnet_cutout',
        'tests.test_execution_runtime.RuntimeTests.test_real_sam2_segment',
        'tests.test_execution_runtime.RuntimeTests.test_real_ocr',
        'tests.test_execution_runtime.RuntimeTests.test_real_sam3_segment',
        'tests.test_native_operation_dispatch.NativeDispatchTests.test_real_cutout_uses_pinned_step_resources_after_claim'])
    result=unittest.TextTestRunner(verbosity=1).run(suite)
    after=source_digest()
    report={'passed':result.wasSuccessful() and before==after,'tests':result.testsRun,'failures':len(result.failures),'errors':len(result.errors),'skipped':len(result.skipped),'source_sha256':after,'source_changed_during_run':before!=after}
    Path('docs/test-evidence/real-models.json').write_text(json.dumps(report,indent=2)+'\n')
    print(json.dumps(report))
    return not report['passed']

if __name__=='__main__':raise SystemExit(main())
