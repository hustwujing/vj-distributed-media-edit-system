#!/usr/bin/env python3
from __future__ import annotations

import importlib
import json
import os
import shutil
import subprocess
from pathlib import Path


ROOT = Path(__file__).resolve().parent / "skills"


def main() -> None:
    manifest = json.loads((ROOT / "manifest.json").read_text())
    commands = set()
    python_modules = set()
    node_modules = set()
    for skill in manifest["skills"]:
        dependencies = skill["dependencies"]
        commands.update(dependencies["commands"])
        python_modules.update(dependencies["python_modules"])
        node_modules.update(dependencies["node_modules"])
    missing_commands = sorted(command for command in commands if not shutil.which(command))
    missing_python = []
    for module in sorted(python_modules):
        try:
            importlib.import_module(module)
        except Exception as exc:
            missing_python.append(f"{module}:{type(exc).__name__}")
    missing_node = []
    for module in sorted(node_modules):
        result = subprocess.run(["node", "-e", f"require({json.dumps(module)})"],
            env={**os.environ,"NODE_PATH":os.environ.get("NODE_PATH","/opt/runtime-assets/node_modules")},
            capture_output=True)
        if result.returncode:
            missing_node.append(module)
    if missing_commands or missing_python or missing_node:
        raise SystemExit(json.dumps({"commands": missing_commands, "python": missing_python,
            "node": missing_node}, indent=2))
    subprocess.run(["editppt", "--help"], check=True, stdout=subprocess.DEVNULL)
    print(json.dumps({"bundle_version": manifest["bundle_version"], "skills": len(manifest["skills"]),
        "commands": len(commands), "python_modules": len(python_modules),
        "node_modules": len(node_modules)}, sort_keys=True))


if __name__ == "__main__":
    main()
