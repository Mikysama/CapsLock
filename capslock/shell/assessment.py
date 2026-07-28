"""Neutral deterministic shell safety classification."""

from __future__ import annotations

import re
import shlex
from dataclasses import dataclass

from .parser import parse_shell


@dataclass(frozen=True)
class ShellAssessment:
    behavior: str
    reason: str
    parsed: tuple[str, ...] = ()
    commands: tuple[str, ...] = ()
    parser_available: bool = True


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
    syntax = parse_shell(command)
    try:
        words = tuple(shlex.split(command, posix=True))
    except ValueError:
        return ShellAssessment("ask", "command could not be parsed reliably")
    if not words:
        return ShellAssessment("ask", "command is empty")
    command_names = syntax.commands
    if any(item in _HARD_DENY_WORDS for item in (*words, *command_names)):
        return ShellAssessment(
            "deny",
            "privilege escalation or sandbox escape command is forbidden",
            words,
            command_names,
            syntax.parser_available,
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
    if not syntax.parser_available:
        return ShellAssessment(
            "ask", "Bash parser is unavailable", words, (), False
        )
    if syntax.has_error:
        return ShellAssessment(
            "ask", "command could not be parsed reliably", words, command_names
        )
    if syntax.dynamic:
        return ShellAssessment(
            "ask",
            "command contains dynamic shell syntax: " + ", ".join(syntax.dynamic),
            words,
            command_names,
        )
    if syntax.redirects:
        return ShellAssessment(
            "ask", "command contains shell redirection", words, command_names
        )
    if command_names and all(item in _SAFE_COMMANDS for item in command_names):
        return ShellAssessment(
            "allow",
            "all command segments are in the deterministic sandbox allowlist",
            words,
            command_names,
        )
    return ShellAssessment(
        "ask", "command requires explicit review", words, command_names
    )


__all__ = ["ShellAssessment", "assess_shell"]
