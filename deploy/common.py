"""Shared, side-effect-free helpers for deployment entry points."""
from __future__ import annotations

import concurrent.futures
import os
from pathlib import Path
import shlex
import subprocess
import sys


def run(args, *, env=None, input=None, check=True, timeout=120, cwd=None):
    result = subprocess.run(
        [str(value) for value in args],
        env=env,
        input=input,
        text=True,
        capture_output=True,
        timeout=timeout,
        cwd=cwd,
    )
    if check and result.returncode:
        # Command input and environment may contain credentials.
        command = Path(str(args[0])).name
        raise RuntimeError(f'{command} failed (exit {result.returncode}); inspect service logs')
    return result


def env_file(path):
    values = {}
    for line in Path(path).read_text().splitlines():
        if not line.strip() or line.lstrip().startswith('#'):
            continue
        key, value = line.split('=', 1)
        parts = shlex.split(value)
        if len(parts) > 1:
            raise ValueError('Invalid environment entry: ' + key)
        values[key] = parts[0] if parts else ''
    return values


def private_env(path, values):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + '.new')
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(descriptor, 'w') as stream:
        for key, value in sorted(values.items()):
            stream.write(key + '=' + shlex.quote(str(value)) + '\n')
    temporary.chmod(0o600)
    temporary.replace(path)


def active(name, *, runner=None):
    runner = runner or run
    return runner(['systemctl', 'is-active', '--quiet', name], check=False).returncode == 0


def parallel_service(action, names, *, runner=None):
    runner = runner or run
    with concurrent.futures.ThreadPoolExecutor(max_workers=len(names) or 1) as pool:
        futures = [pool.submit(runner, ['systemctl', action, name], timeout=150) for name in names]
        for future in futures:
            future.result()


def digest(root, *, runner=None):
    root = Path(root)
    runner = runner or run
    command = [sys.executable, '-c', 'from tests.run_workflow_suite import source_digest; print(source_digest())']
    environment = {**os.environ, 'PYTHONPATH': str(root)}
    if root == Path.cwd():
        result = runner(command, env=environment, timeout=30)
    else:
        result = subprocess.run(
            command,
            cwd=root,
            env=environment,
            capture_output=True,
            text=True,
            check=True,
        )
    return result.stdout.strip()
