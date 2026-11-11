#!/usr/bin/env python3
from __future__ import annotations

import argparse
import hashlib
import json
import re
from pathlib import Path


ROOT = Path(__file__).resolve().parent / "skills"
SOURCES = ROOT / "sources.json"
MANIFEST = ROOT / "manifest.json"
NAME = re.compile(r"^name:\s*['\"]?([a-z0-9][a-z0-9-]*)", re.MULTILINE)
IGNORED_PARTS = {"__pycache__", ".pytest_cache"}
IGNORED_SUFFIXES = {".pyc", ".pyo"}


def tree_digest(root: Path) -> tuple[str, int]:
    digest = hashlib.sha256()
    count = 0
    for path in sorted(
        value
        for value in root.rglob("*")
        if value.is_file()
        and not IGNORED_PARTS.intersection(value.relative_to(root).parts)
        and value.suffix not in IGNORED_SUFFIXES
    ):
        relative = path.relative_to(root).as_posix()
        digest.update(relative.encode() + b"\0")
        digest.update(hashlib.sha256(path.read_bytes()).digest())
        count += 1
    return digest.hexdigest(), count


def expanded(source_file: Path = SOURCES, skill_root: Path = ROOT) -> dict:
    source = json.loads(source_file.read_text())
    profiles = source["dependency_profiles"]
    rows = []
    seen = set()
    for upstream in source["sources"]:
        for declared in upstream["skills"]:
            skill_id = declared["id"]
            if skill_id in seen:
                raise ValueError("Duplicate skill ID: " + skill_id)
            seen.add(skill_id)
            directory = skill_root / skill_id
            entry = directory / "SKILL.md"
            if not entry.is_file():
                raise ValueError("Missing Skill entry: " + str(entry))
            match = NAME.search(entry.read_text(errors="strict")[:16_384])
            if not match or match.group(1) != skill_id:
                raise ValueError(f"Skill directory/name mismatch for {skill_id}")
            profile_name = declared["profile"]
            if profile_name not in profiles:
                raise ValueError("Unknown dependency profile: " + profile_name)
            content_sha256, file_count = tree_digest(directory)
            rows.append({
                **declared,
                "source": upstream["id"],
                "source_url": upstream["url"],
                "source_commit": upstream["commit"],
                "license": declared.get("license", upstream["license"]),
                "supported_harnesses": ["codex", "claude", "deepseek"],
                "entry": "SKILL.md",
                "dependencies": profiles[profile_name],
                "content_sha256": content_sha256,
                "file_count": file_count,
            })
    return {
        "schema_version": source["schema_version"],
        "bundle_version": source["bundle_version"],
        "retired_skills": source.get("retired_skills", []),
        "skills": sorted(rows, key=lambda value: value["id"]),
    }


def freeze() -> None:
    value = expanded()
    MANIFEST.write_text(json.dumps(value, indent=2, ensure_ascii=False) + "\n")
    print(f"froze {len(value['skills'])} skills in {MANIFEST}")


def verify() -> None:
    expected = json.loads(MANIFEST.read_text())
    actual = expanded()
    if actual != expected:
        raise SystemExit("Runtime Skill bundle manifest is stale; run runtime/skill_bundle.py freeze")
    print(f"runtime skill bundle ok: {len(actual['skills'])} skills, version {actual['bundle_version']}")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("action", choices=("freeze", "verify"))
    args = parser.parse_args()
    freeze() if args.action == "freeze" else verify()


if __name__ == "__main__":
    main()
