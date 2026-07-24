"""Neutral deterministic shell safety classification."""

from __future__ import annotations

import re
import shlex
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class ShellAssessment:
    behavior: str
    reason: str
    parsed: tuple[str, ...] = ()


_HARD_DENY_WORDS = {
    "sudo",
    "su",
    "doas",
    "mount",
    "umount",
    "nsenter",
    "unshare",
    "chroot",
    "pivot_root",
    "modprobe",
    "insmod",
    "rmmod",
    "mkfs",
    "fdisk",
    "parted",
    "swapon",
    "swapoff",
}
_SAFE_COMMANDS = {
    "git",
    "pytest",
    "ruff",
    "python",
    "python3",
    "npm",
    "pnpm",
    "yarn",
    "cargo",
    "go",
    "make",
    "cmake",
    "ninja",
    "rg",
    "grep",
    "find",
    "ls",
    "pwd",
    "sed",
    "awk",
    "head",
    "tail",
    "wc",
    "sort",
    "uniq",
    "diff",
}


def assess_shell(command: str) -> ShellAssessment:
    if not command.strip() or "\x00" in command or "\n" in command:
        return ShellAssessment(
            "ask", "command is empty or contains unsupported control data"
        )
    if re.search(r"(?:^|\s)/(?:dev|proc|sys)(?:/|\s|$)", command):
        return ShellAssessment(
            "deny", "device and kernel filesystem access is forbidden"
        )
    if re.search(
        r"\brm\s+(?:-[^\s]*r[^\s]*f|-[^\s]*f[^\s]*r)\s+(?:/|~|\.\.?\b|\*|\$\{|\$[A-Za-z])",
        command,
    ):
        return ShellAssessment(
            "deny", "broad recursive deletion has an unresolved or dangerous target"
        )
    if re.search(r"\b(?:curl|wget)\b[^|;&]*(?:\||>)\s*(?:sh|bash)\b", command):
        return ShellAssessment(
            "deny", "downloading and directly executing code is forbidden"
        )
    try:
        words = tuple(shlex.split(command, posix=True))
    except ValueError:
        return ShellAssessment("ask", "command could not be parsed reliably")
    if not words:
        return ShellAssessment("ask", "command is empty")
    if any(item in _HARD_DENY_WORDS for item in words):
        return ShellAssessment(
            "deny", "privilege escalation or sandbox escape command is forbidden", words
        )
    if any(
        marker in token for token in words for marker in ("/dev/", "/proc/", "/sys/")
    ):
        return ShellAssessment(
            "deny", "device and kernel filesystem access is forbidden", words
        )
    if re.search(r"\b(?:chmod|chown)\b[^;&]*(?:\s/\s*$|\s/\*)", command):
        return ShellAssessment(
            "deny", "recursive ownership or mode changes at root are forbidden", words
        )
    if any(marker in command for marker in ("$(", "`", "${", "<<")):
        return ShellAssessment("ask", "command contains dynamic shell expansion", words)
    commands: list[str] = []
    for segment in re.split(r"\s*(?:&&|\|\||;|\|)\s*", command):
        try:
            parsed = shlex.split(segment)
        except ValueError:
            return ShellAssessment("ask", "compound command could not be parsed", words)
        while parsed and re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*=.*", parsed[0]):
            parsed.pop(0)
        if parsed:
            commands.append(Path(parsed[0]).name)
    if commands and all(item in _SAFE_COMMANDS for item in commands):
        return ShellAssessment(
            "allow",
            "all command segments are in the deterministic sandbox allowlist",
            words,
        )
    return ShellAssessment("ask", "command requires explicit review", words)


__all__ = ["ShellAssessment", "assess_shell"]
