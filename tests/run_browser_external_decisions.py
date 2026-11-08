"""Run the focused decision UI check with explicit, offline browser dependencies.

No application/database/worker is started. The Node driver serves its own local
fixture and mocks only the command API; shipped UI code handles real DOM clicks.
"""
import argparse
import json
import os
from pathlib import Path
import subprocess
import tempfile


def main():
    repository=Path(__file__).resolve().parents[1]
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--node-modules',type=Path,help='Directory containing installed playwright and playwright-core')
    parser.add_argument('--browser-cache',type=Path,help='Existing Playwright Chromium cache; no downloads are performed')
    parser.add_argument('--output',type=Path)
    args=parser.parse_args()
    if args.node_modules and not (args.node_modules/'playwright/package.json').is_file():
        parser.error('--node-modules must contain the installed playwright package')
    from tests.isolation import isolate_environment,isolate_network,temporary_root
    root=temporary_root().resolve()
    output=(args.output or root/'external-decisions-browser.json').resolve()
    if not output.is_relative_to(repository.parent):
        parser.error('Evidence must stay below the workspace parent (on this host /data2/wujing03)')
    output.parent.mkdir(parents=True,exist_ok=True)
    browsers=(args.browser_cache or Path(os.environ.get('PLAYWRIGHT_BROWSERS_PATH') or repository.parent/'tool-cache/ms-playwright')).resolve()
    if not browsers.is_dir():parser.error('Browser cache is missing; pass --browser-cache with an existing Chromium cache')
    with tempfile.TemporaryDirectory(prefix='decision-browser-',dir=root) as temporary:
        isolate_environment(Path(temporary));isolate_network()
        environment={**os.environ,'PLAYWRIGHT_BROWSERS_PATH':str(browsers)}
        if args.node_modules:environment['NODE_PATH']=str(args.node_modules.resolve())
        probe=subprocess.run(['node','-e',
            "const p=require('playwright/package.json');console.log(JSON.stringify({playwright:p.version,module:require.resolve('playwright')}))"],
            cwd=repository,env=environment,capture_output=True,text=True,timeout=10)
        if probe.returncode:
            parser.error('Playwright Node package is missing. Install the pinned package from docs/development.md, or pass --node-modules; browser binaries alone are insufficient.')
        result=subprocess.run(['node',str(repository/'tests/browser_external_decisions.cjs'),str(output)],
            cwd=repository,env=environment,capture_output=True,text=True,timeout=90)
        if result.returncode:
            print(result.stdout);print(result.stderr)
            return result.returncode
        report=json.loads(result.stdout)
        report['runtime']={**json.loads(probe.stdout),'browser_cache':str(browsers),
                           'network_isolated':environment.get('VIDEO_USE_TEST_NETWORK_ISOLATED')=='1'}
        output.write_text(json.dumps(report,ensure_ascii=False,indent=2)+'\n')
        print(json.dumps(report,ensure_ascii=False))
    return 0


if __name__=='__main__':raise SystemExit(main())
