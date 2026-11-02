"""Run integration cases with isolated data and a newly staged sandbox package."""
import json
import os
from pathlib import Path
import shutil
import sys
import tempfile
import time
import unittest
from tests.suite_lock import serialized


@serialized
def main():
    started=time.monotonic()
    sys.path.insert(0,str(Path('tests').resolve()))
    from tests.run_workflow_suite import source_digest
    before=source_digest()
    with tempfile.TemporaryDirectory(prefix='vt-') as folder:
        root=Path(folder);root.chmod(0o755)
        from tests.isolation import isolate_environment,isolate_network
        isolate_environment(root,root/'runtime')
        isolate_network()
        from webapp import runtime_image
        runtime_image.MANIFEST=root/'runtime/manifest.json'
        runtime_image.MANIFEST.parent.mkdir(mode=0o755)
        original=Path('runtime/build/manifest.json')
        if original.is_file():
            shutil.copyfile(original,runtime_image.MANIFEST)
            runtime_image.MANIFEST.chmod(0o644)
            runtime_image.stage_tools(runtime_image.MANIFEST.parent)
        names=sys.argv[1:]
        suite=(unittest.defaultTestLoader.loadTestsFromNames(names) if names
               else unittest.defaultTestLoader.discover('tests',pattern='test_*.py'))
        result=unittest.TextTestRunner(verbosity=1).run(suite)
        after=source_digest()
        report={'passed':result.wasSuccessful() and before==after,'tests':result.testsRun,'failures':len(result.failures),
                'errors':len(result.errors),'skipped':len(result.skipped),'seconds':round(time.monotonic()-started,2),
                'source_sha256':after,'source_changed_during_run':before!=after,
                'selection':names or ['all Python tests'],
                'infrastructure':'Distributed cases use isolated real PostgreSQL, an HTTP S3 protocol fixture, distinct node roots, real subprocesses and Linux sandboxes; no live paid provider calls',
                'skipped_tests':[{'test':str(test),'reason':reason} for test,reason in result.skipped],
                'failed_tests':[str(t) for t,_ in result.failures+result.errors]}
        target=Path('docs/test-evidence/distributed-suite.json');target.parent.mkdir(parents=True,exist_ok=True)
        target.write_text(json.dumps(report,ensure_ascii=False,indent=2)+'\n')
        print(json.dumps(report,ensure_ascii=False))
        return not report['passed']


if __name__=='__main__':raise SystemExit(main())
