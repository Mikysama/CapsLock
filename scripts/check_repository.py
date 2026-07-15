#!/usr/bin/env python3
"""Reject tracked local state, credentials, caches, and build output."""

from __future__ import annotations

import subprocess
from pathlib import PurePosixPath


def tracked_files() -> list[PurePosixPath]:
    result = subprocess.run(
        ["git", "ls-files", "-z"],
        check=True,
        capture_output=True,
        text=True,
    )
    return [PurePosixPath(item) for item in result.stdout.split("\0") if item]


def forbidden_reason(path: PurePosixPath) -> str | None:
    parts = set(path.parts)
    name = path.name
    if (name == ".env" or name.startswith(".env.")) and name != ".env.example":
        return "environment file"
    if name.endswith((".swp", ".swo")) or name == ".DS_Store":
        return "editor or operating-system artifact"
    if ".capslock" in parts or any(part == ".venv" or part.startswith(".venv-") for part in parts):
        return "runtime or virtual-environment state"
    if parts.intersection({"__pycache__", ".pytest_cache", ".ruff_cache", "build", "dist", "htmlcov"}):
        return "cache or build output"
    if any(part.endswith(".egg-info") for part in parts):
        return "generated package metadata"
    if name in {".coverage", "coverage.xml"}:
        return "coverage output"
    return None


def main() -> int:
    failures = [(path, forbidden_reason(path)) for path in tracked_files()]
    failures = [(path, reason) for path, reason in failures if reason is not None]
    if not failures:
        print("Repository hygiene check passed.")
        return 0
    for path, reason in failures:
        print(f"Forbidden tracked file ({reason}): {path}")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
