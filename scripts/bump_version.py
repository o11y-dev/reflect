#!/usr/bin/env python3
"""Bump version in pyproject.toml and stamp CHANGELOG.md with a release date."""

from __future__ import annotations

import re
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PYPROJECT = ROOT / "pyproject.toml"
CHANGELOG = ROOT / "CHANGELOG.md"


def bump_pyproject(version: str) -> None:
    text = PYPROJECT.read_text(encoding="utf-8")
    updated = re.sub(
        r'^version\s*=\s*"[^"]+"',
        f'version = "{version}"',
        text,
        count=1,
        flags=re.MULTILINE,
    )
    if updated == text:
        print(f"warning: version string not found in {PYPROJECT}", file=sys.stderr)
    PYPROJECT.write_text(updated, encoding="utf-8")


def stamp_changelog(version: str) -> None:
    if not CHANGELOG.exists():
        return
    text = CHANGELOG.read_text(encoding="utf-8")
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    updated = re.sub(
        rf"## {re.escape(version)} \(unreleased\)",
        f"## {version} ({today})",
        text,
        count=1,
    )
    if updated == text:
        print(f"warning: no unreleased entry for {version} in CHANGELOG.md", file=sys.stderr)
    CHANGELOG.write_text(updated, encoding="utf-8")


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if len(args) != 1:
        print("usage: bump_version.py <version>", file=sys.stderr)
        return 2
    version = args[0]
    if not re.match(r"^\d+\.\d+\.\d+$", version):
        print(f"error: version must be semver (e.g. 0.3.0), got: {version}", file=sys.stderr)
        return 1
    bump_pyproject(version)
    stamp_changelog(version)
    print(f"Bumped to {version}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
