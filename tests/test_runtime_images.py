from pathlib import Path
import subprocess
import unittest


ROOT=Path(__file__).resolve().parents[1]


class RuntimeImageDefinitionTests(unittest.TestCase):
    def test_images_require_digest_bases_offline_dependencies_and_nonroot_runtime(self):
        folder=ROOT/'runtime/opensandbox'
        foundation=(folder/'foundation.Dockerfile').read_text()
        self.assertIn('PYTHON_IMAGE must be immutable',foundation)
        self.assertIn('ffmpeg',foundation)
        for package in ('file','procps','socat','graphviz','libreoffice-core','pandoc','poppler-utils','qpdf','tesseract-ocr'):
            self.assertIn(package,foundation)
        self.assertIn('drawio-amd64-32.6.10.deb',foundation)
        self.assertIn('fonts-wqy-zenhei',foundation)
        base=(folder/'base.Dockerfile').read_text()
        self.assertIn('BASE_IMAGE must be immutable',base)
        self.assertIn('pip install --no-index',base)
        for dependency in ('networkx','PyYAML','sympy','xmltodict'):
            self.assertIn(dependency,(ROOT/'pyproject.toml').read_text())
        self.assertIn('FROM ${BASE_IMAGE} AS runtime-builder',base)
        self.assertIn('COPY --from=runtime-builder /opt/venv',base)
        self.assertIn('COPY runtime/skills /opt/video-use/skills',base)
        self.assertIn('sandbox-skill-smoke',base)
        self.assertIn('COPY references /opt/video-use/references',base)
        self.assertIn('COPY helpers/edit_media.py',base)
        self.assertIn('find /opt/video-use -type f -exec chmod 0444',base)
        self.assertIn('/usr/local/bin',base)
        self.assertIn('/usr/sbin',base)
        self.assertIn('USER 10001:10001',base)
        self.assertIn('webapp.runtime_bootstrap',base)
        expected={'claude':'claude-agent-sdk==0.2.176',
                  'deepseek':'deepseek-harness-sdk==0.2.1rc2'}
        for name in ('codex','claude','deepseek'):
            value=(folder/(name+'.Dockerfile')).read_text()
            self.assertIn('RUNTIME_BASE',value);self.assertIn('@sha256:',value)
            if name in expected:self.assertIn(expected[name],value)
            if name == 'codex':self.assertIn('chmod 0755 /opt/codex',value)
            if name in expected:self.assertIn('COPY --from=harness-builder /opt/harness',value)
        combined='\n'.join(path.read_text() for path in folder.glob('*Dockerfile'))
        for secret in ('API_KEY=','SECRET_ACCESS_KEY=','DATABASE_URL='):
            self.assertNotIn(secret,combined)

    def test_build_script_has_offline_runtime_phases_and_is_shell_valid(self):
        script=ROOT/'deploy/build-agent-runtime.sh';value=script.read_text()
        self.assertIn('--network=none',value)
        self.assertIn('foundation|base|codex|claude|deepseek|harnesses',value)
        self.assertIn('${DOCKER:-docker}',value)
        subprocess.run(['/bin/bash','-n',str(script)],check=True)


if __name__=='__main__':unittest.main()
