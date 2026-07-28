"""Tree-sitter backed Bash syntax analysis for deterministic policy checks."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import shlex
from typing import Any


@dataclass(frozen=True)
class ShellSegment:
    argv: tuple[str, ...]
    operator_before: str | None = None


@dataclass(frozen=True)
class ShellSyntax:
    commands: tuple[str, ...] = ()
    segments: tuple[ShellSegment, ...] = ()
    redirects: tuple[str, ...] = ()
    operators: tuple[str, ...] = ()
    dynamic: tuple[str, ...] = ()
    has_error: bool = False
    parser_available: bool = True

    @property
    def simple(self) -> bool:
        return not self.redirects and not self.operators and not self.dynamic


_DYNAMIC_NODES = {
    "command_substitution": "command substitution",
    "process_substitution": "process substitution",
    "simple_expansion": "parameter expansion",
    "expansion": "parameter expansion",
    "arithmetic_expansion": "arithmetic expansion",
    "heredoc_redirect": "heredoc",
    "herestring_redirect": "herestring",
}
_OPERATOR_NODES = {"&&", "||", ";", "&", "|", "|&"}
_REDIRECT_NODES = {"file_redirect", "heredoc_redirect", "herestring_redirect"}


class TreeSitterShellParser:
    """Parse Bash without executing it; unavailable parsers fail closed."""

    def __init__(self) -> None:
        self._parser: Any | None = None
        try:
            from tree_sitter import Language, Parser
            import tree_sitter_bash

            self._parser = Parser(Language(tree_sitter_bash.language()))
        except (ImportError, TypeError, ValueError):
            self._parser = None

    def parse(self, command: str) -> ShellSyntax:
        if self._parser is None:
            return ShellSyntax(parser_available=False, has_error=True)
        encoded = command.encode("utf-8")
        try:
            root = self._parser.parse(encoded).root_node
        except Exception:
            return ShellSyntax(has_error=True)
        commands: list[str] = []
        redirects: list[str] = []
        operators: list[str] = []
        operator_spans: list[tuple[int, int, str]] = []
        dynamic: list[str] = []
        has_error = bool(root.has_error)

        stack = [root]
        while stack:
            node = stack.pop()
            kind = str(node.type)
            if kind == "ERROR" or getattr(node, "is_missing", False):
                has_error = True
            if kind == "command_name":
                value = encoded[node.start_byte : node.end_byte].decode(
                    "utf-8", errors="replace"
                )
                if value and not any(marker in value for marker in ("$", "`", "(")):
                    commands.append(Path(value).name)
                else:
                    dynamic.append("dynamic command name")
            if kind in _REDIRECT_NODES:
                redirects.append(
                    encoded[node.start_byte : node.end_byte].decode(
                        "utf-8", errors="replace"
                    )
                )
            if kind in _DYNAMIC_NODES:
                dynamic.append(_DYNAMIC_NODES[kind])
            if kind == "&":
                dynamic.append("background execution")
            if kind in _OPERATOR_NODES:
                operators.append(kind)
                operator_spans.append((node.start_byte, node.end_byte, kind))
            stack.extend(reversed(node.children))
        segments: list[ShellSegment] = []
        cursor = 0
        operator_before: str | None = None
        for start, end, operator in sorted(set(operator_spans)):
            raw = encoded[cursor:start].decode("utf-8", errors="replace").strip()
            if raw:
                try:
                    segments.append(
                        ShellSegment(tuple(shlex.split(raw)), operator_before)
                    )
                except ValueError:
                    has_error = True
            cursor = end
            operator_before = operator
        raw = encoded[cursor:].decode("utf-8", errors="replace").strip()
        if raw:
            try:
                segments.append(ShellSegment(tuple(shlex.split(raw)), operator_before))
            except ValueError:
                has_error = True
        return ShellSyntax(
            tuple(commands),
            tuple(segments),
            tuple(redirects),
            tuple(operators),
            tuple(dict.fromkeys(dynamic)),
            has_error,
            True,
        )


_DEFAULT_PARSER = TreeSitterShellParser()


def parse_shell(command: str) -> ShellSyntax:
    return _DEFAULT_PARSER.parse(command)


__all__ = ["ShellSegment", "ShellSyntax", "TreeSitterShellParser", "parse_shell"]
