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
    read_only_workspace: bool = False


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
_READ_ONLY_GIT_COMMANDS = {"status", "diff", "log", "show", "rev-parse", "ls-files"}
_UNSAFE_GIT_OPTIONS = {
    "--ext-diff",
    "--no-index",
    "--output",
    "--textconv",
    "--exec-path",
    "--git-dir",
    "--work-tree",
    "--config-env",
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
        return ShellAssessment("ask", "Bash parser is unavailable", words, (), False)
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
    if _read_only_segments(syntax.segments):
        return ShellAssessment(
            "allow",
            "all command segments are in the deterministic read-only allowlist",
            words,
            command_names,
            syntax.parser_available,
            True,
        )
    return ShellAssessment(
        "ask", "command requires explicit review", words, command_names
    )


def _read_only_segments(segments) -> bool:
    if not segments:
        return False
    for index, segment in enumerate(segments):
        argv = segment.argv
        if not argv:
            return False
        command = argv[0]
        if command == "pwd":
            if len(argv) != 1 or segment.operator_before == "|":
                return False
            continue
        if command == "git":
            if segment.operator_before == "|" or not _read_only_git(argv):
                return False
            continue
        if command in {"head", "tail", "wc", "uniq"}:
            if index == 0 or segment.operator_before != "|":
                return False
            if not _stdin_filter(command, argv[1:]):
                return False
            continue
        return False
    return True


def _read_only_git(argv: tuple[str, ...]) -> bool:
    index = 1
    if index < len(argv) and argv[index] == "--no-pager":
        index += 1
    if index >= len(argv) or argv[index] not in _READ_ONLY_GIT_COMMANDS:
        return False
    for value in argv[index + 1 :]:
        if value in {"-c", "-C"} or value.startswith(("-c=", "-C=")):
            return False
        if value in _UNSAFE_GIT_OPTIONS or value.startswith(
            (
                "--output",
                "--ext-diff=",
                "--textconv=",
                "--exec-path=",
                "--git-dir=",
                "--work-tree=",
                "--config-env=",
            )
        ):
            return False
    return True


def _stdin_filter(command: str, arguments: tuple[str, ...]) -> bool:
    if command in {"head", "tail"}:
        index = 0
        while index < len(arguments):
            value = arguments[index]
            if re.fullmatch(r"-\d+", value):
                index += 1
                continue
            if value in {"-n", "--lines", "-c", "--bytes"}:
                if index + 1 >= len(arguments) or not _count(arguments[index + 1]):
                    return False
                index += 2
                continue
            if value.startswith(("--lines=", "--bytes=")) and _count(
                value.split("=", 1)[1]
            ):
                index += 1
                continue
            return False
        return True
    if command == "wc":
        return all(
            re.fullmatch(r"-[cmlwL]+", value)
            or value
            in {"--bytes", "--chars", "--lines", "--max-line-length", "--words"}
            for value in arguments
        )
    return all(
        re.fullmatch(r"-[cdiu]+", value)
        or value in {"--count", "--repeated", "--ignore-case", "--unique"}
        for value in arguments
    )


def _count(value: str) -> bool:
    return re.fullmatch(r"[+-]?\d+", value) is not None


__all__ = ["ShellAssessment", "assess_shell"]
