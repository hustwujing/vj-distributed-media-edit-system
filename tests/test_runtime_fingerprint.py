import json
import os
from pathlib import Path
import shutil
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from webapp import agent_sandbox,runtime_image


class RuntimeFingerprintTests(unittest.TestCase):
    def test_gpu_task_adds_only_scheduler_assigned_devices_and_identity(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory)
            env={key:root/key for key in ('project','agent','state','workspace','preview_root','preview','sources','transcripts')}
            env.update(mode='plan',uid=os.getuid(),gid=os.getgid(),preview_path='/workspace/edit/previews/test',
                       runtime=SimpleNamespace(gpu_index=2,legacy_paths=False,read_gid=os.getgid(),wrap=lambda args,scope:args))
            mounts=['--perms','0755','--dir','/dev','--dev-bind','/dev/nvidia2','/dev/nvidia2']
            with patch.object(runtime_image,'mounts',return_value=[]), \
                 patch('webapp.gpu_sandbox_mounts.arguments',return_value=(mounts,'GPU-fixture')):
                command=agent_sandbox.command(env,None,executable=['/usr/bin/true'])
            self.assertIn('/dev/nvidia2',command);self.assertNotIn('/dev/nvidia1',command)
            at=command.index('CUDA_VISIBLE_DEVICES');self.assertEqual(command[at+1],'GPU-fixture')

    def test_every_actual_host_module_mount_changes_the_fingerprint_when_modified(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);repository=root/'repo'
            shutil.copytree(runtime_image.REPO/'webapp',repository/'webapp',ignore=shutil.ignore_patterns('__pycache__'))
            manifest=root/'build/manifest.json';manifest.parent.mkdir()
            manifest.write_text(json.dumps({'version':2,'python_version':'3.13','packages':[],'harnesses':{},'data_files':{}}))
            env={key:root/key for key in ('project','agent','state','workspace','preview_root','preview','sources','transcripts')}
            env.update(mode='plan',uid=os.getuid(),gid=os.getgid(),preview_path='/workspace/edit/previews/test')
            with patch.object(runtime_image,'mounts',return_value=[]):
                command=agent_sandbox.command(env,None,executable=['/usr/bin/true'])
            mounted=[(Path(command[index+1]).name,command[index+2]) for index,word in enumerate(command)
                     if word=='--ro-bind' and Path(command[index+1]).parent==agent_sandbox.REPO/'webapp']
            self.assertEqual(set(mounted),set(runtime_image.MOUNTED_MODULES))
            self.assertEqual(len(mounted),len(set(mounted)))
            with patch.object(runtime_image,'REPO',repository),patch.object(runtime_image,'MANIFEST',manifest):
                baseline=runtime_image.fingerprint()
                for name,_ in mounted:
                    with self.subTest(module=name):
                        path=repository/'webapp'/name;original=path.read_bytes()
                        path.write_bytes(original+b'\n# changed trusted module\n')
                        self.assertNotEqual(runtime_image.fingerprint(),baseline)
                        path.write_bytes(original)
                        self.assertEqual(runtime_image.fingerprint(),baseline)
                helper=repository/'webapp/gpu_sandbox_mounts.py';original=helper.read_bytes()
                helper.write_bytes(original+b'\n# GPU projection policy changed\n')
                self.assertNotEqual(runtime_image.fingerprint(),baseline)
                helper.write_bytes(original);self.assertEqual(runtime_image.fingerprint(),baseline)
                changed=tuple((name,target+'.changed' if index==0 else target)
                              for index,(name,target) in enumerate(runtime_image.MOUNTED_MODULES))
                with patch.object(runtime_image,'MOUNTED_MODULES',changed):
                    self.assertNotEqual(runtime_image.fingerprint(),baseline)
