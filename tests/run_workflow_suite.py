"""File-backed suite entrypoint: multiprocessing.spawn cannot reload <stdin>."""
import hashlib
import json
from pathlib import Path
import time
import tempfile
import os
import unittest
from tests.suite_lock import serialized


def source_digest():
    paths=sorted([*Path('webapp').rglob('*.py'),*Path('helpers').glob('*.py'),*Path('runtime/public').glob('*'),*Path('runtime/opensandbox').glob('*'),Path('runtime/assets-manifest.json'),*Path('references').glob('*.md'),*Path('tests').glob('*.py'),*Path('tests').glob('*.mjs'),*Path('tests').glob('*.cjs'),*Path('webapp/static').glob('*.js'),*Path('webapp/static').glob('*.css'),*Path('webapp/static').glob('*.html'),*Path('deploy').glob('*.sh'),*Path('deploy').glob('*.py'),*Path('deploy').glob('*.service'),Path('pyproject.toml'),Path('uv.lock')])
    value=hashlib.sha256()
    paths+= [Path('pyproject.toml'),Path('uv.lock'),Path('deploy.sh'),*sorted(Path('deploy').glob('*.py'))]
    for path in paths:
        # Long-running QA drivers are versioned separately by their campaign.
        # They are not imported by the app or the unittest discovery suite.
        if path.parent==Path('tests') and (path.name.startswith('campaign_') or path.name=='real_case_browser.cjs'):continue
        if path.is_file():value.update(str(path).encode());value.update(path.read_bytes())
    return value.hexdigest()


@serialized
def main():
    started=time.time();before=source_digest()
    isolated_default=tempfile.TemporaryDirectory(prefix='vu-suite-default-')
    from tests.isolation import isolate_environment,isolate_network
    isolate_environment(Path(isolated_default.name))
    isolate_network()
    previous_data=os.environ.get('VIDEO_USE_DATA_DIR')
    os.environ['VIDEO_USE_DATA_DIR']=isolated_default.name
    suite=unittest.defaultTestLoader.discover('tests',pattern='test_*.py')
    try:result=unittest.TextTestRunner(verbosity=1).run(suite)
    finally:
        if previous_data is None:os.environ.pop('VIDEO_USE_DATA_DIR',None)
        else:os.environ['VIDEO_USE_DATA_DIR']=previous_data
        isolated_default.cleanup()
    after=source_digest()
    report={'passed':result.wasSuccessful() and before==after,'tests':result.testsRun,'failures':len(result.failures),'errors':len(result.errors),'skipped':len(result.skipped),
        'seconds':round(time.time()-started,2),'source_sha256':after,'source_changed_during_run':before!=after,
        'scope':'Python suite including real local models; live external providers have separate evidence' if os.environ.get('VIDEO_USE_TEST_REAL_MODELS')=='1' else 'Python suite; real models and live providers have separate evidence',
        'failed_tests':[str(t) for t,_ in result.failures+result.errors]}
    Path('docs/test-evidence').mkdir(exist_ok=True)
    Path('docs/test-evidence/overall-result.json').write_text(json.dumps(report,ensure_ascii=False,indent=2)+'\n')
    print(json.dumps(report,ensure_ascii=False))
    return not report['passed']

if __name__=='__main__':raise SystemExit(main())
