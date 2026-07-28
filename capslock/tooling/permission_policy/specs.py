"""Tool-specific permission normalization and matching strategies."""

from __future__ import annotations

import json
import re
import shlex
from pathlib import PurePosixPath
from typing import Any
from urllib.parse import urlsplit, urlunsplit

from .models import (
    PermissionBehavior,
    PermissionDestination,
    PermissionRule,
    PermissionUpdate,
    PermissionUpdateOperation,
)
from ..contracts import ExecutionContext, ResolvedToolPolicy, ToolDefinition


class DefaultPermissionSpec:
    allowed_constraints: frozenset[str] = frozenset()

    def normalize(
        self, tool: ToolDefinition, arguments: dict[str, Any], context: ExecutionContext
    ) -> dict[str, Any]:
        del tool, context
        return json.loads(json.dumps(arguments))

    def hard_check(
        self,
        tool: ToolDefinition,
        arguments: dict[str, Any],
        policy: ResolvedToolPolicy,
        context: ExecutionContext,
    ) -> tuple[PermissionBehavior, str, str] | None:
        del tool, arguments, policy, context
        return None

    def matches(
        self,
        rule: PermissionRule,
        tool: ToolDefinition,
        arguments: dict[str, Any],
    ) -> bool:
        if not _tool_matches(rule, tool.name):
            return False
        return all(
            arguments.get(key) == value for key, value in rule.constraints.items()
        )

    def suggest_updates(
        self, tool: ToolDefinition, arguments: dict[str, Any]
    ) -> tuple[PermissionUpdate, ...]:
        del arguments
        return _allow_suggestions(tool.name, {})


class FilePermissionSpec(DefaultPermissionSpec):
    allowed_constraints = frozenset({"path"})

    def normalize(
        self, tool: ToolDefinition, arguments: dict[str, Any], context: ExecutionContext
    ) -> dict[str, Any]:
        normalized = super().normalize(tool, arguments, context)
        path = normalized.get("path")
        if isinstance(path, str):
            resolved = context.policy.resolve(path)
            normalized["path"] = (
                resolved.relative_to(context.policy.root).as_posix() or "."
            )
        return normalized

    def hard_check(self, tool, arguments, policy, context):
        del tool, policy
        path = arguments.get("path")
        if isinstance(path, str):
            try:
                context.policy.resolve(path)
            except Exception as exc:
                return PermissionBehavior.DENY, "workspace_boundary", str(exc)
        return None

    def matches(self, rule, tool, arguments):
        if not _tool_matches(rule, tool.name):
            return False
        expected = rule.constraints.get("path")
        if expected is None:
            return not rule.constraints
        actual = arguments.get("path")
        patterns = expected if isinstance(expected, list) else [expected]
        return isinstance(actual, str) and any(
            _path_glob_match(actual, str(pattern)) for pattern in patterns
        )

    def suggest_updates(self, tool, arguments):
        path = arguments.get("path")
        return _allow_suggestions(
            tool.name, {"path": path} if isinstance(path, str) else {}
        )


class ShellPermissionSpec(DefaultPermissionSpec):
    allowed_constraints = frozenset(
        {"command", "command_prefix", "cwd", "sandbox", "network"}
    )

    def normalize(self, tool, arguments, context):
        normalized = super().normalize(tool, arguments, context)
        command = normalized.get("command")
        if isinstance(command, str):
            normalized["command"] = command.strip()
        cwd = normalized.get("cwd", ".")
        if isinstance(cwd, str):
            directory = context.policy.command_directory(cwd)
            normalized["cwd"] = (
                directory.relative_to(context.policy.root).as_posix() or "."
            )
        normalized["sandbox"] = normalized.get("sandbox", "default")
        normalized["network"] = sorted(set(normalized.get("network", [])))
        return normalized

    def hard_check(self, tool, arguments, policy, context):
        del tool, context
        if policy.destructive:
            return (
                PermissionBehavior.DENY,
                "dangerous_shell_command",
                "deterministic shell analysis rejected the command",
            )
        if arguments.get("sandbox") != "default":
            return (
                PermissionBehavior.DENY,
                "host_execution_forbidden",
                "host shell execution is outside the automatic permission boundary",
            )
        network = arguments.get("network", [])
        if network not in ([], ["*"]):
            return (
                PermissionBehavior.DENY,
                "unenforceable_network_scope",
                "the sandbox can enforce only no network or unrestricted network",
            )
        command = arguments.get("command")
        if not isinstance(command, str) or not command:
            return PermissionBehavior.ASK, "shell_unparseable", "shell command is empty"
        if arguments.get("background") is True:
            return (
                PermissionBehavior.ASK,
                "background_process_confirmation",
                "background processes require an explicit approval",
            )
        return None

    def matches(self, rule, tool, arguments):
        if not _tool_matches(rule, tool.name):
            return False
        command = str(arguments.get("command", ""))
        segments = _shell_segments(command)
        if rule.behavior is PermissionBehavior.ALLOW and segments is None:
            return False
        if "command" in rule.constraints:
            expected = rule.constraints["command"]
            values = expected if isinstance(expected, list) else [expected]
            if command not in {str(item).strip() for item in values}:
                return False
            if rule.behavior is PermissionBehavior.ALLOW and len(segments or ()) != 1:
                return False
        if "command_prefix" in rule.constraints:
            expected = rule.constraints["command_prefix"]
            prefixes = expected if isinstance(expected, list) else [expected]
            if segments is None:
                return False
            matcher = all if rule.behavior is PermissionBehavior.ALLOW else any
            if not matcher(
                any(_command_prefix_match(segment, str(prefix)) for prefix in prefixes)
                for segment in segments
            ):
                return False
        cwd = rule.constraints.get("cwd")
        if cwd is not None:
            patterns = cwd if isinstance(cwd, list) else [cwd]
            if not any(
                _path_glob_match(str(arguments.get("cwd", ".")), str(pattern))
                for pattern in patterns
            ):
                return False
        if (
            "sandbox" in rule.constraints
            and arguments.get("sandbox") != rule.constraints["sandbox"]
        ):
            return False
        if "network" in rule.constraints:
            requested = set(str(item) for item in arguments.get("network", []))
            allowed = set(str(item) for item in rule.constraints["network"])
            if not requested.issubset(allowed):
                return False
        return True

    def suggest_updates(self, tool, arguments):
        constraints = {
            "command": arguments.get("command", ""),
            "cwd": arguments.get("cwd", "."),
            "sandbox": arguments.get("sandbox", "default"),
            "network": list(arguments.get("network", [])),
        }
        return _allow_suggestions(tool.name, constraints)


class WebPermissionSpec(DefaultPermissionSpec):
    allowed_constraints = frozenset({"host", "operation"})

    def normalize(self, tool, arguments, context):
        normalized = super().normalize(tool, arguments, context)
        url = normalized.get("url")
        if isinstance(url, str):
            parsed = urlsplit(url)
            if parsed.scheme.lower() not in {"http", "https"} or not parsed.hostname:
                raise ValueError("web URL must use public HTTP or HTTPS")
            host = parsed.hostname.encode("idna").decode("ascii").lower()
            port = parsed.port
            default_port = 80 if parsed.scheme.lower() == "http" else 443
            netloc = host if port in {None, default_port} else f"{host}:{port}"
            normalized["url"] = urlunsplit(
                (parsed.scheme.lower(), netloc, parsed.path or "/", parsed.query, "")
            )
        return normalized

    def matches(self, rule, tool, arguments):
        if not _tool_matches(rule, tool.name):
            return False
        operation = rule.constraints.get("operation")
        if operation is not None and operation != tool.name:
            return False
        host = rule.constraints.get("host")
        if host is None:
            return not set(rule.constraints) - {"operation"}
        actual = _url_host(arguments.get("url"))
        patterns = host if isinstance(host, list) else [host]
        return isinstance(actual, str) and any(
            _host_match(actual, str(pattern)) for pattern in patterns
        )

    def suggest_updates(self, tool, arguments):
        host = _url_host(arguments.get("url"))
        constraints = {"operation": tool.name}
        if isinstance(host, str):
            constraints["host"] = host
        return _allow_suggestions(tool.name, constraints)


class McpPermissionSpec(DefaultPermissionSpec):
    allowed_constraints = frozenset({"server", "mcp_tool"})

    @staticmethod
    def identity(name: str) -> tuple[str, str]:
        parts = name.split("__", 2)
        return (parts[1], parts[2]) if len(parts) == 3 else ("", name)

    def matches(self, rule, tool, arguments):
        del arguments
        if not _tool_matches(rule, tool.name):
            return False
        server, mcp_tool = self.identity(tool.name)
        return (
            rule.constraints.get("server", server) == server
            and rule.constraints.get("mcp_tool", mcp_tool) == mcp_tool
        )

    def suggest_updates(self, tool, arguments):
        del arguments
        server, mcp_tool = self.identity(tool.name)
        return _allow_suggestions(tool.name, {"server": server, "mcp_tool": mcp_tool})


def _tool_matches(rule: PermissionRule, name: str) -> bool:
    if rule.matcher_version == 1:
        import fnmatch

        return fnmatch.fnmatchcase(name, rule.tool)
    return name == rule.tool


def _path_glob_match(path: str, pattern: str) -> bool:
    normalized_path = path.replace("\\", "/").removeprefix("./") or "."
    normalized_pattern = pattern.replace("\\", "/").removeprefix("./") or "."
    if (
        PurePosixPath(normalized_pattern).is_absolute()
        or ".." in PurePosixPath(normalized_pattern).parts
    ):
        return False
    regex = ""
    index = 0
    while index < len(normalized_pattern):
        char = normalized_pattern[index]
        if char == "*":
            if (
                index + 1 < len(normalized_pattern)
                and normalized_pattern[index + 1] == "*"
            ):
                if (
                    index + 2 < len(normalized_pattern)
                    and normalized_pattern[index + 2] == "/"
                ):
                    regex += "(?:.*/)?"
                    index += 3
                else:
                    regex += ".*"
                    index += 2
                continue
            regex += "[^/]*"
        elif char == "?":
            regex += "[^/]"
        else:
            regex += re.escape(char)
        index += 1
    return re.fullmatch(regex, normalized_path) is not None


def _shell_segments(command: str) -> tuple[str, ...] | None:
    if any(marker in command for marker in ("$(", "`", "${", "\n", "\x00")):
        return None
    raw_segments = re.split(r"\s*(?:&&|\|\||;|\|)\s*", command)
    segments: list[str] = []
    for raw in raw_segments:
        try:
            words = shlex.split(raw, posix=True)
        except ValueError:
            return None
        if not words:
            return None
        if any(re.search(r"(?:^|\d)[<>]|[<>](?:&|\|)?", word) for word in words):
            return None
        segments.append(" ".join(shlex.quote(item) for item in words))
    return tuple(segments)


def _command_prefix_match(command: str, prefix: str) -> bool:
    try:
        command_words = shlex.split(command, posix=True)
        prefix_words = shlex.split(prefix.strip(), posix=True)
    except ValueError:
        return False
    return bool(prefix_words) and command_words[: len(prefix_words)] == prefix_words


def _host_match(host: str, pattern: str) -> bool:
    normalized = pattern.encode("idna").decode("ascii").lower()
    if normalized.startswith("*."):
        suffix = normalized[2:]
        return host.endswith("." + suffix) and host != suffix
    return host == normalized


def _url_host(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    try:
        parsed = urlsplit(value)
        return (
            parsed.hostname.encode("idna").decode("ascii").lower()
            if parsed.hostname
            else None
        )
    except (UnicodeError, ValueError):
        return None


def _allow_suggestions(
    tool: str, constraints: dict[str, object]
) -> tuple[PermissionUpdate, ...]:
    return tuple(
        PermissionUpdate(
            PermissionUpdateOperation.ADD,
            destination,
            PermissionBehavior.ALLOW,
            tool,
            json.loads(json.dumps(constraints)),
        )
        for destination in (PermissionDestination.SESSION, PermissionDestination.LOCAL)
    )


__all__ = [
    "DefaultPermissionSpec",
    "FilePermissionSpec",
    "McpPermissionSpec",
    "ShellPermissionSpec",
    "WebPermissionSpec",
]
