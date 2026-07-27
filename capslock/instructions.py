"""Controlled, read-only repository instruction discovery."""

from __future__ import annotations

import fnmatch
import hashlib
import os
import re
from dataclasses import dataclass
from pathlib import Path

MAX_FILE_BYTES = 40 * 1024
MAX_TOTAL_TOKENS = 12_000


@dataclass(frozen=True)
class InstructionRecord:
    path: Path
    sha256: str
    scope: str
    match_rule: str | None
    token_count: int
    priority: int
    loaded: bool = True
    diagnostic: str | None = None


@dataclass(frozen=True)
class InstructionBundle:
    text: str
    records: tuple[InstructionRecord, ...]
    digest: str


class InstructionLoader:
    """Discover bounded instruction files without includes or symlink traversal."""

    def __init__(self, workspace: Path) -> None:
        self.workspace = workspace.resolve()
        self.last = InstructionBundle("", (), hashlib.sha256(b"").hexdigest())

    def load(self, current: Path | None = None) -> InstructionBundle:
        current = (current or self.workspace).resolve()
        if not current.is_relative_to(self.workspace):
            current = self.workspace
        candidates = self._candidates(current)
        records: list[InstructionRecord] = []
        accepted: list[tuple[int, Path, str, InstructionRecord]] = []
        for priority, path, scope, rule in candidates:
            record, content = self._read(path, scope, rule, priority)
            records.append(record)
            if content is not None:
                accepted.append((priority, path, content, record))
        remaining = MAX_TOTAL_TOKENS
        selected: set[Path] = set()
        # Highest priority wins when the total budget is exceeded.
        for _, path, _, record in sorted(
            accepted, key=lambda item: item[0], reverse=True
        ):
            if record.token_count <= remaining:
                selected.add(path)
                remaining -= record.token_count
        final_records = []
        for record in records:
            if record.loaded and record.path not in selected:
                record = InstructionRecord(
                    **{
                        **record.__dict__,
                        "loaded": False,
                        "diagnostic": "instruction token budget exceeded",
                    }
                )
            final_records.append(record)
        blocks = [
            f"# Instructions: {path}\n{content}"
            for _, path, content, _ in sorted(accepted, key=lambda item: item[0])
            if path in selected
        ]
        text = "\n\n".join(blocks)
        digest = hashlib.sha256(
            "\n".join(
                f"{record.path}|{record.sha256}|{int(record.loaded)}"
                for record in final_records
            ).encode()
        ).hexdigest()
        self.last = InstructionBundle(text, tuple(final_records), digest)
        return self.last

    def _candidates(self, current: Path) -> list[tuple[int, Path, str, str | None]]:
        output: list[tuple[int, Path, str, str | None]] = []
        user = Path(os.environ.get("CAPSLOCK_HOME", Path.home() / ".capslock"))
        output.append((0, user / "CAPSLOCK.md", "user", None))
        chain = [self.workspace]
        relative = current.relative_to(self.workspace)
        cursor = self.workspace
        for part in relative.parts:
            cursor /= part
            if cursor.is_dir():
                chain.append(cursor)
        priority = 10
        for directory in chain:
            output.append((priority, directory / "AGENTS.md", "directory", None))
            output.append((priority + 1, directory / "CAPSLOCK.md", "directory", None))
            priority += 10
        rules = self.workspace / ".capslock" / "rules"
        if rules.is_dir() and not rules.is_symlink():
            rel = current.relative_to(self.workspace).as_posix() or "."
            for path in sorted(rules.glob("*.md")):
                rule = _path_rule(path)
                if rule is None or any(_matches_path(rel, pattern) for pattern in rule):
                    output.append(
                        (priority + 5, path, "path-rule", ",".join(rule or ("**",)))
                    )
        output.append(
            (
                priority + 20,
                self.workspace / ".capslock" / "local" / "CAPSLOCK.md",
                "local",
                None,
            )
        )
        return output

    def _read(
        self, path: Path, scope: str, rule: str | None, priority: int
    ) -> tuple[InstructionRecord, str | None]:
        empty_hash = hashlib.sha256(b"").hexdigest()
        if not path.exists():
            return InstructionRecord(
                path, empty_hash, scope, rule, 0, priority, False, "not found"
            ), None
        if path.is_symlink():
            return InstructionRecord(
                path, empty_hash, scope, rule, 0, priority, False, "symlink rejected"
            ), None
        try:
            raw = path.read_bytes()
        except OSError as exc:
            return InstructionRecord(
                path, empty_hash, scope, rule, 0, priority, False, type(exc).__name__
            ), None
        digest = hashlib.sha256(raw).hexdigest()
        if len(raw) > MAX_FILE_BYTES:
            return InstructionRecord(
                path, digest, scope, rule, 0, priority, False, "file exceeds 40 KiB"
            ), None
        try:
            content = raw.decode("utf-8")
        except UnicodeDecodeError:
            return InstructionRecord(
                path, digest, scope, rule, 0, priority, False, "invalid UTF-8"
            ), None
        if re.search(r"(?m)^\s*@include\b", content):
            return InstructionRecord(
                path, digest, scope, rule, 0, priority, False, "@include rejected"
            ), None
        tokens = max(1, (len(content) + 3) // 4)
        return InstructionRecord(path, digest, scope, rule, tokens, priority), content


def _path_rule(path: Path) -> tuple[str, ...] | None:
    try:
        text = path.read_text(encoding="utf-8")[:4096]
    except (OSError, UnicodeDecodeError):
        return None
    match = re.match(r"\A---\s*\n(.*?)\n---\s*\n", text, re.DOTALL)
    if not match:
        return None
    paths = re.search(r"(?m)^paths\s*:\s*(.+)$", match.group(1))
    if not paths:
        return None
    value = paths.group(1).strip().strip("[]")
    return tuple(item.strip().strip("'\"") for item in value.split(",") if item.strip())


def _matches_path(relative: str, pattern: str) -> bool:
    return fnmatch.fnmatch(relative, pattern) or (
        pattern.endswith("/**") and relative == pattern[:-3].rstrip("/")
    )
