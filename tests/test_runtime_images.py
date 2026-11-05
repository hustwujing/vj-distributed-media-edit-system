from pathlib import Path
import subprocess
import unittest


ROOT=Path(__file__).resolve().parents[1]


class RuntimeImageDefinitionTests(unittest.TestCase):
    def test_images_require_digest_bases_offline_dependencies_and_nonroot_runtime(self):
        folder=ROOT/'runtime/opensandbox'
        base=(folder/'base.Dockerfile').read_text()
        self.assertIn('BASE_IMAGE must be immutable',base)
        self.assertIn('pip install --no-index',base)
        self.assertIn('USER 10001:10001',base)
        self.assertIn('webapp.runtime_bootstrap',base)
        expected={'claude':'claude-agent-sdk==0.2.176',
                  'deepseek':'deepseek-harness-sdk==0.2.1rc2'}
        for name in ('codex','claude','deepseek'):
            value=(folder/(name+'.Dockerfile')).read_text()
            self.assertIn('RUNTIME_BASE',value);self.assertIn('@sha256:',value)
            if name in expected:self.assertIn(expected[name],value)
        combined='\n'.join(path.read_text() for path in folder.glob('*Dockerfile'))
        for secret in ('API_KEY=','SECRET_ACCESS_KEY=','DATABASE_URL='):
            self.assertNotIn(secret,combined)

    def test_build_script_is_two_phase_and_shell_valid(self):
        script=ROOT/'deploy/build-agent-runtime.sh';value=script.read_text()
        self.assertIn('base|harnesses',value);self.assertIn('--network=none',value)
        subprocess.run(['/bin/bash','-n',str(script)],check=True)


if __name__=='__main__':unittest.main()
