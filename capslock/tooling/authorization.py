"""Structured, explainable, tool-aware permission rules and middleware."""

from __future__ import annotations

import hashlib
import json
import re
import shlex
import tomllib
import uuid
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path, PurePosixPath
from typing import Any, Iterable, Protocol
from urllib.parse import urlsplit, urlunsplit

import tomlkit

from ..permissions import PermissionMode
from .contracts import (
    ExecutionContext,
    ResolvedToolPolicy,
    ToolDefinition,
    ToolMiddleware,
    ToolOutcome,
    ToolOutcomeStatus,
    ToolPause,
)


PERMISSIONS_VERSION = 2
_SOURCE_PRIORITY = {"user": 0, "project": 1, "local": 2, "session": 3}
_PATH_KEYS = {"path", "cwd"}
_FILE_TOOLS = {
    "list_files",
    "glob_files",
    "read_file",
    "read_image",
    "search_files",
    "edit_file",
    "create_file",
    "write_file",
    "edit_notebook",
    "read_pdf",
    "read_notebook",
    "read_tool_artifact",
}
_ACTION_TOOLS = {
    "edit_file",
    "create_file",
    "shell",
    "run_check",
    "web_search",
    "web_fetch",
    "write_file",
    "edit_notebook",
    "create_worktree",
    "exit_worktree",
}


class PermissionBehavior(StrEnum):
    ALLOW = "allow"
    ASK = "ask"
    DENY = "deny"


class PermissionDestination(StrEnum):
    SESSION = "session"
    LOCAL = "local"
    PROJECT = "project"
    USER = "user"


class PermissionUpdateOperation(StrEnum):
    ADD = "add"
    REMOVE = "remove"


@dataclass(frozen=True)
class PermissionUpdate:
    operation: PermissionUpdateOperation
    destination: PermissionDestination
    behavior: PermissionBehavior
    tool: str
    constraints: dict[str, object] = field(default_factory=dict)
    rule_id: str | None = None

    def as_dict(self) -> dict[str, object]:
        return {
            "operation": self.operation.value,
            "destination": self.destination.value,
            "behavior": self.behavior.value,
            "tool": self.tool,
            "constraints": json.loads(json.dumps(self.constraints)),
            "rule_id": self.rule_id,
        }


class PermissionSpec(Protocol):
    """Tool-owned interpretation of permission-relevant input."""

    def normalize(
        self, tool: ToolDefinition, arguments: dict[str, Any], context: ExecutionContext
    ) -> dict[str, Any]: ...

    def hard_check(
        self,
        tool: ToolDefinition,
        arguments: dict[str, Any],
        policy: ResolvedToolPolicy,
        context: ExecutionContext,
    ) -> tuple[PermissionBehavior, str, str] | None: ...

    def matches(
        self,
        rule: "PermissionRule",
        tool: ToolDefinition,
        arguments: dict[str, Any],
    ) -> bool: ...

    def suggest_updates(
        self, tool: ToolDefinition, arguments: dict[str, Any]
    ) -> tuple[PermissionUpdate, ...]: ...


@dataclass(frozen=True)
class PermissionRule:
    behavior: PermissionBehavior
    tool: str
    constraints: dict[str, object]
    source: str
    identifier: str | None = None
    matcher_version: int = PERMISSIONS_VERSION
    diagnostic: str | None = None

    def as_dict(self) -> dict[str, object]:
        return {
            "id": self.identifier,
            "behavior": self.behavior.value,
            "tool": self.tool,
            "constraints": json.loads(json.dumps(self.constraints)),
            "source": self.source,
            "matcher_version": self.matcher_version,
            "diagnostic": self.diagnostic,
        }

    @property
    def specificity(self) -> tuple[int, int, int]:
        exact = sum(
            1
            for key, value in self.constraints.items()
            if key not in {"path", "cwd", "command_prefix", "host"}
            or isinstance(value, str) and not _contains_glob(value)
        )
        literal = sum(
            len(str(value).replace("*", "")) for value in self.constraints.values()
        )
        return exact, len(self.constraints), literal


@dataclass(frozen=True)
class PermissionDecision:
    behavior: PermissionBehavior
    source: str
    reason: str
    reason_code: str
    mode: PermissionMode
    rule: PermissionRule | None = None
    normalized_arguments_sha256: str = ""
    input_modified: bool = False
    suggestions: tuple[PermissionUpdate, ...] = ()
    decided_by: str | None = None
    classifier: dict[str, object] | None = None

    @property
    def persistence_suggestion(self) -> str | None:
        return self.suggestions[0].destination.value if self.suggestions else None

    def as_dict(self, *, include_rule: bool = True) -> dict[str, object]:
        return {
            "behavior": self.behavior.value,
            "source": self.source,
            "reason": self.reason,
            "reason_code": self.reason_code,
            "mode": self.mode.value,
            "normalized_arguments_sha256": self.normalized_arguments_sha256,
            "input_modified": self.input_modified,
            "suggestions": [item.as_dict() for item in self.suggestions],
            "decided_by": self.decided_by,
            "classifier": self.classifier,
            **({"rule": self.rule.as_dict() if self.rule else None} if include_rule else {}),
        }


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
        return all(arguments.get(key) == value for key, value in rule.constraints.items())

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
            normalized["path"] = resolved.relative_to(context.policy.root).as_posix() or "."
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
            normalized["cwd"] = directory.relative_to(context.policy.root).as_posix() or "."
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
        if "sandbox" in rule.constraints and arguments.get("sandbox") != rule.constraints["sandbox"]:
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
        return _allow_suggestions(
            tool.name, {"server": server, "mcp_tool": mcp_tool}
        )


class PermissionEngine:
    """Merge immutable boundaries, v2 file rules, and session grants."""

    def __init__(self, paths: Iterable[tuple[str, Path]], repository: Any) -> None:
        self.paths = tuple(paths)
        self.repository = repository
        self._diagnostics: list[str] = []

    def spec_for(self, tool: ToolDefinition) -> PermissionSpec:
        if tool.name in _FILE_TOOLS:
            return FilePermissionSpec()
        if tool.name in {"shell", "run_check"}:
            return ShellPermissionSpec()
        if tool.name in {"web_search", "web_fetch"}:
            return WebPermissionSpec()
        if tool.name.startswith("mcp__"):
            return McpPermissionSpec()
        return DefaultPermissionSpec()

    def normalize(
        self, tool: ToolDefinition, arguments: dict[str, Any], context: ExecutionContext
    ) -> dict[str, Any]:
        return self.spec_for(tool).normalize(tool, arguments, context)

    async def decide(
        self,
        tool: ToolDefinition,
        arguments: dict[str, Any],
        policy: ResolvedToolPolicy,
        context: ExecutionContext,
    ) -> PermissionDecision:
        spec = self.spec_for(tool)
        normalized = spec.normalize(tool, arguments, context)
        digest = _arguments_digest(normalized)
        hard = spec.hard_check(tool, normalized, policy, context)
        if hard:
            behavior, code, reason = hard
            return PermissionDecision(
                behavior,
                "safety_boundary",
                reason,
                code,
                context.permission_mode,
                normalized_arguments_sha256=digest,
                suggestions=(),
                decided_by="safety",
            )

        rules = list(await self.rules(context.session_id))
        matching = [rule for rule in rules if spec.matches(rule, tool, normalized)]
        if (
            not any(rule.behavior is PermissionBehavior.DENY for rule in matching)
            and context.invocation_id
            and hasattr(self.repository, "consume_permission_grant")
            and await self.repository.consume_permission_grant(
                session_id=context.session_id,
                run_id=context.run_id,
                invocation_id=context.invocation_id,
                tool=tool.name,
                arguments_sha256=digest,
            )
        ):
            return PermissionDecision(
                PermissionBehavior.ALLOW,
                "one_time_grant",
                "consumed a one-time approval bound to this invocation and input",
                "one_time_grant",
                context.permission_mode,
                normalized_arguments_sha256=digest,
                decided_by="user",
            )
        for behavior in (
            PermissionBehavior.DENY,
            PermissionBehavior.ASK,
            PermissionBehavior.ALLOW,
        ):
            candidates = [item for item in matching if item.behavior is behavior]
            if not candidates:
                continue
            selected = max(
                candidates,
                key=lambda item: (
                    item.specificity,
                    _SOURCE_PRIORITY.get(item.source, -1),
                    item.identifier or "",
                ),
            )
            if behavior is PermissionBehavior.ALLOW and selected.source == "project":
                trusted = await self._project_allow_is_trusted()
                if not trusted:
                    return PermissionDecision(
                        PermissionBehavior.ASK,
                        "project",
                        "project allow rule requires trust for the current file digest",
                        "project_allow_untrusted",
                        context.permission_mode,
                        selected,
                        digest,
                        suggestions=(),
                        decided_by="trust_boundary",
                    )
            return PermissionDecision(
                behavior,
                selected.source,
                f"matched {selected.source} permission rule for {selected.tool}",
                f"explicit_{behavior.value}",
                context.permission_mode,
                selected,
                digest,
                suggestions=(),
                decided_by="rule",
            )

        classifier = context.runtime_state.get("shell_classifier")
        if (
            context.permission_mode is PermissionMode.APPROVE_FOR_ME
            and tool.name == "shell"
            and context.runtime_state.get("classifier_auto_allow") is True
            and isinstance(classifier, dict)
            and classifier.get("honored") is True
            and float(classifier.get("confidence", 0)) >= 0.95
            and normalized.get("sandbox") == "default"
            and normalized.get("network") == []
            and normalized.get("background", False) is False
        ):
            return PermissionDecision(
                PermissionBehavior.ALLOW,
                "shell_classifier",
                "fast classifier allowed an otherwise unknown command inside deterministic sandbox boundaries",
                "classifier_allow",
                context.permission_mode,
                normalized_arguments_sha256=digest,
                decided_by="routing.fast",
                classifier=classifier,
            )

        if (
            context.permission_mode is PermissionMode.APPROVE_FOR_ME
            and tool.name == "shell"
            and context.runtime_state.get("shell_deterministic_behavior") == "allow"
            and normalized.get("sandbox") == "default"
            and normalized.get("network") == []
            and normalized.get("background", False) is False
        ):
            default = PermissionBehavior.ALLOW
        elif context.permission_mode is PermissionMode.FULL_ACCESS:
            default = PermissionBehavior.ALLOW
        elif context.permission_mode is PermissionMode.ASK_FOR_APPROVAL:
            default = PermissionBehavior.ASK
        elif policy.destructive or policy.external_side_effects or policy.open_world:
            default = PermissionBehavior.ASK
        else:
            default = PermissionBehavior.ALLOW
        return PermissionDecision(
            default,
            "permission_mode",
            f"defaulted from permission mode {context.permission_mode.value}",
            "mode_default",
            context.permission_mode,
            normalized_arguments_sha256=digest,
            suggestions=spec.suggest_updates(tool, normalized)
            if default is PermissionBehavior.ASK
            else (),
            decided_by="mode",
        )

    async def rules(self, session_id: str) -> tuple[PermissionRule, ...]:
        self._diagnostics.clear()
        rules = list(self._file_rules())
        if hasattr(self.repository, "session_permission_rules"):
            for raw in await self.repository.session_permission_rules(session_id):
                rules.append(_parse_rule(raw, "session", PERMISSIONS_VERSION))
        seen: dict[tuple[object, ...], PermissionRule] = {}
        for rule in rules:
            signature = (
                rule.behavior.value,
                rule.tool,
                json.dumps(rule.constraints, sort_keys=True, default=str),
            )
            previous = seen.get(signature)
            if previous is not None:
                self._diagnostics.append(
                    f"duplicate permission rules {previous.identifier or '-'} and "
                    f"{rule.identifier or '-'}"
                )
            else:
                seen[signature] = rule
        for rule in rules:
            if rule.behavior is not PermissionBehavior.ALLOW:
                continue
            blocker = next(
                (
                    candidate
                    for candidate in rules
                    if candidate.behavior
                    in {PermissionBehavior.DENY, PermissionBehavior.ASK}
                    and _rules_overlap(candidate, rule)
                ),
                None,
            )
            if blocker is not None:
                self._diagnostics.append(
                    f"allow rule {rule.identifier or '-'} is shadowed by "
                    f"{blocker.behavior.value} rule {blocker.identifier or '-'}"
                )
        if any(
            rule.source == "project"
            and rule.behavior is PermissionBehavior.ALLOW
            for rule in rules
        ) and not await self._project_allow_is_trusted():
            self._diagnostics.append(
                "project allow rules are inactive until the current permissions file digest is trusted"
            )
        return tuple(rules)

    async def has_explicit_restriction(
        self,
        *,
        session_id: str,
        tool: str,
        arguments: dict[str, Any],
        context: ExecutionContext,
    ) -> bool:
        proxy = type("PermissionToolProxy", (), {"name": tool})()
        spec = self.spec_for(proxy)
        normalized = spec.normalize(proxy, arguments, context)
        return any(
            rule.behavior in {PermissionBehavior.ASK, PermissionBehavior.DENY}
            and spec.matches(rule, proxy, normalized)
            for rule in await self.rules(session_id)
        )

    def diagnostics(self) -> tuple[str, ...]:
        return tuple(self._diagnostics)

    async def apply_update(self, session_id: str, update: PermissionUpdate) -> str:
        if update.operation is PermissionUpdateOperation.ADD:
            _validate_rule(
                PermissionRule(
                    update.behavior,
                    update.tool,
                    update.constraints,
                    update.destination.value,
                    matcher_version=PERMISSIONS_VERSION,
                )
            )
        if update.destination is PermissionDestination.SESSION:
            if update.operation is PermissionUpdateOperation.REMOVE:
                if not update.rule_id or not hasattr(self.repository, "remove_session_permission_rule"):
                    raise ValueError("session rule removal is unavailable")
                await self.repository.remove_session_permission_rule(session_id, update.rule_id)
                return update.rule_id
            return await self.repository.add_session_permission_rule(
                session_id,
                behavior=update.behavior.value,
                tool=update.tool,
                constraints=update.constraints,
                matcher_version=PERMISSIONS_VERSION,
            )
        path = dict(self.paths).get(update.destination.value)
        if path is None:
            raise ValueError(f"permission destination is unavailable: {update.destination.value}")
        return _persist_file_update(path, update)

    async def verify_explicit_allow(
        self,
        *,
        session_id: str,
        tool: str,
        arguments: dict[str, Any],
    ) -> bool:
        proxy = type("PermissionToolProxy", (), {"name": tool})()
        spec = self.spec_for(proxy)
        matching = [
            rule
            for rule in await self.rules(session_id)
            if spec.matches(rule, proxy, arguments)
        ]
        if any(rule.behavior is PermissionBehavior.DENY for rule in matching):
            return False
        if any(rule.behavior is PermissionBehavior.ASK for rule in matching):
            return False
        return any(rule.behavior is PermissionBehavior.ALLOW for rule in matching)

    async def trust_project_permissions(self) -> str:
        digest = self._project_digest()
        if digest is None:
            raise ValueError("project permissions file does not exist")
        if not hasattr(self.repository, "set_permission_setting"):
            raise ValueError("project permission trust storage is unavailable")
        await self.repository.set_permission_setting("project_permissions_sha256", digest)
        return digest

    async def _project_allow_is_trusted(self) -> bool:
        digest = self._project_digest()
        if digest is None or not hasattr(self.repository, "permission_setting"):
            return False
        return await self.repository.permission_setting("project_permissions_sha256") == digest

    def _project_digest(self) -> str | None:
        path = dict(self.paths).get("project")
        if path is None or not path.is_file():
            return None
        return hashlib.sha256(path.read_bytes()).hexdigest()

    def _file_rules(self) -> Iterable[PermissionRule]:
        for source, path in self.paths:
            if not path.is_file():
                continue
            try:
                document = tomllib.loads(path.read_text(encoding="utf-8"))
            except (OSError, UnicodeError, tomllib.TOMLDecodeError) as exc:
                self._diagnostics.append(f"invalid {source} permissions file {path}: {exc}")
                yield PermissionRule(
                    PermissionBehavior.ASK,
                    "*",
                    {},
                    source,
                    matcher_version=1,
                    diagnostic="invalid permission file; all automatic grants are disabled",
                )
                continue
            version = document.get("permissions_version", 1)
            if not isinstance(version, int) or version not in {1, PERMISSIONS_VERSION}:
                self._diagnostics.append(f"unsupported permissions version in {path}: {version}")
                yield PermissionRule(
                    PermissionBehavior.ASK, "*", {}, source, matcher_version=1
                )
                continue
            raw_rules = document.get("rules", document.get("rule", []))
            if not isinstance(raw_rules, list):
                self._diagnostics.append(f"permissions rules must be an array in {path}")
                yield PermissionRule(
                    PermissionBehavior.ASK, "*", {}, source, matcher_version=1
                )
                continue
            for index, raw in enumerate(raw_rules):
                if not isinstance(raw, dict):
                    self._diagnostics.append(f"invalid rule {index + 1} in {path}")
                    yield PermissionRule(
                        PermissionBehavior.ASK,
                        "*",
                        {},
                        source,
                        matcher_version=1,
                        diagnostic="invalid rule; all automatic grants are disabled",
                    )
                    continue
                try:
                    rule = _parse_rule(raw, source, version)
                    _validate_rule(rule)
                except ValueError as exc:
                    self._diagnostics.append(f"invalid rule {index + 1} in {path}: {exc}")
                    yield PermissionRule(
                        PermissionBehavior.ASK,
                        "*",
                        {},
                        source,
                        matcher_version=1,
                        diagnostic="invalid rule; all automatic grants are disabled",
                    )
                    continue
                if version == 1 and rule.behavior is PermissionBehavior.ALLOW and _old_allow_is_ambiguous(rule):
                    message = f"legacy allow rule {rule.identifier or index + 1} in {path} was downgraded to ask"
                    self._diagnostics.append(message)
                    rule = PermissionRule(
                        PermissionBehavior.ASK,
                        rule.tool,
                        rule.constraints,
                        rule.source,
                        rule.identifier,
                        1,
                        message,
                    )
                yield rule


class PermissionMiddleware(ToolMiddleware):
    def __init__(self, engine: PermissionEngine) -> None:
        self.engine = engine

    async def normalize(self, tool, arguments, context):
        return self.engine.normalize(tool, arguments, context)

    async def authorize(self, tool, arguments, policy, context):
        if context.runtime_state.pop("planning_control_authorized", False):
            return None
        decision = await self.engine.decide(tool, arguments, policy, context)
        context.runtime_state["permission_decision"] = decision.as_dict()
        context.event(
            "tool_permission",
            name=tool.name,
            behavior=decision.behavior.value,
            source=decision.source,
            reason=decision.reason,
            reason_code=decision.reason_code,
        )
        permission_emit = context.runtime_state.get("permission_emit")
        if callable(permission_emit):
            await permission_emit(decision.as_dict(include_rule=False))
        if context.invocation_id and hasattr(self.engine.repository, "record_permission_decision"):
            await self.engine.repository.record_permission_decision(
                invocation_id=context.invocation_id,
                behavior=decision.behavior.value,
                source=decision.source,
                reason=decision.reason,
                reason_code=decision.reason_code,
                mode=decision.mode.value,
                arguments_sha256=decision.normalized_arguments_sha256,
                rule=_audit_rule(decision.rule) if decision.rule else None,
                classifier=decision.classifier,
                decided_by=decision.decided_by,
                suggestions=_audit_suggestions(decision.suggestions),
            )
        if decision.behavior is PermissionBehavior.DENY:
            return ToolOutcome(
                ToolOutcomeStatus.DENIED,
                False,
                error=decision.reason,
                error_code="permission_denied",
            )
        if decision.behavior is PermissionBehavior.ASK:
            if tool.name in _ACTION_TOOLS or tool.name.startswith(("plugin__", "mcp__")):
                context.runtime_state["force_manual_approval"] = True
                return None
            if hasattr(self.engine.repository, "create_permission_request") and context.invocation_id:
                request_id = await self.engine.repository.create_permission_request(
                    session_id=context.session_id,
                    run_id=context.run_id,
                    invocation_id=context.invocation_id,
                    tool=tool.name,
                    arguments_sha256=decision.normalized_arguments_sha256,
                    reason=decision.reason,
                    suggestions=[item.as_dict() for item in decision.suggestions],
                )
            else:
                request_id = f"permission:{context.invocation_id or tool.name}"
            return ToolPause(
                "approval",
                request_id,
                {
                    "tool": tool.name,
                    "reason": decision.reason,
                    "reason_code": decision.reason_code,
                    "arguments": arguments,
                    "suggestions": [item.as_dict() for item in decision.suggestions],
                },
                {"permission_source": decision.source},
            )
        return None

    async def after(self, tool, arguments, policy, outcome, context):
        del tool, arguments, policy, context
        return outcome


def _parse_rule(raw: dict[str, Any], source: str, version: int) -> PermissionRule:
    try:
        behavior = PermissionBehavior(str(raw["behavior"]))
        tool = str(raw["tool"])
    except (KeyError, ValueError) as exc:
        raise ValueError(f"invalid {source} permission rule") from exc
    constraints = raw.get("constraints", raw.get("constraint", {}))
    if not isinstance(constraints, dict):
        raise ValueError(f"permission constraint must be a table in {source}")
    if version == 1 and tool.startswith("mcp__") and "tool" in constraints:
        constraints = dict(constraints)
        constraints["mcp_tool"] = constraints.pop("tool")
    identifier = str(raw["id"]) if raw.get("id") is not None else _rule_digest(
        behavior.value, tool, constraints, source
    )
    return PermissionRule(
        behavior,
        tool,
        json.loads(json.dumps(constraints)),
        source,
        identifier,
        version,
    )


def _validate_rule(rule: PermissionRule) -> None:
    if not rule.tool or "\x00" in rule.tool:
        raise ValueError("tool name must be non-empty and contain no NUL")
    if rule.matcher_version == PERMISSIONS_VERSION and _contains_glob(rule.tool):
        raise ValueError("v2 tool names must be exact")
    if rule.tool in _FILE_TOOLS:
        allowed = FilePermissionSpec.allowed_constraints
    elif rule.tool in {"shell", "run_check"}:
        allowed = ShellPermissionSpec.allowed_constraints
    elif rule.tool in {"web_search", "web_fetch"}:
        allowed = WebPermissionSpec.allowed_constraints
    elif rule.tool.startswith("mcp__"):
        allowed = McpPermissionSpec.allowed_constraints
    else:
        allowed = DefaultPermissionSpec.allowed_constraints
    unknown = set(rule.constraints) - set(allowed)
    if unknown:
        raise ValueError(f"unknown constraints for {rule.tool}: {sorted(unknown)}")
    for key in _PATH_KEYS & set(rule.constraints):
        values = rule.constraints[key]
        for value in values if isinstance(values, list) else [values]:
            text = str(value).replace("\\", "/")
            if "\x00" in text or PurePosixPath(text).is_absolute() or ".." in PurePosixPath(text).parts:
                raise ValueError(f"unsafe {key} constraint")
            if "[" in text or "]" in text:
                raise ValueError(
                    f"unsupported or invalid {key} glob; use *, **, or ?"
                )
    if "network" in rule.constraints:
        value = rule.constraints["network"]
        if not isinstance(value, list) or any(item not in {"*"} for item in value):
            raise ValueError("network constraint must be [] or ['*']")
    if "command" in rule.constraints and "command_prefix" in rule.constraints:
        raise ValueError("command and command_prefix are mutually exclusive")


def _old_allow_is_ambiguous(rule: PermissionRule) -> bool:
    return _contains_glob(rule.tool) or any(
        _contains_glob(str(value))
        for key, raw in rule.constraints.items()
        if key != "network"
        for value in (raw if isinstance(raw, list) else [raw])
    )


def _tool_matches(rule: PermissionRule, name: str) -> bool:
    if rule.matcher_version == 1:
        import fnmatch

        return fnmatch.fnmatchcase(name, rule.tool)
    return name == rule.tool


def _rules_overlap(blocker: PermissionRule, allowed: PermissionRule) -> bool:
    if not (
        blocker.tool == allowed.tool
        or blocker.matcher_version == 1
        and blocker.tool == "*"
    ):
        return False
    if not blocker.constraints:
        return True
    return blocker.constraints == allowed.constraints


def _arguments_digest(arguments: dict[str, Any]) -> str:
    public = {key: value for key, value in arguments.items() if not key.startswith("_permission_")}
    encoded = json.dumps(public, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _audit_rule(rule: PermissionRule) -> dict[str, object]:
    constraints = json.loads(json.dumps(rule.constraints))
    if "command" in constraints:
        constraints["command_sha256"] = hashlib.sha256(
            json.dumps(constraints.pop("command"), sort_keys=True).encode("utf-8")
        ).hexdigest()
    return {
        "id": rule.identifier,
        "behavior": rule.behavior.value,
        "tool": rule.tool,
        "source": rule.source,
        "matcher_version": rule.matcher_version,
        "constraints_summary": constraints,
    }


def _audit_suggestions(
    suggestions: tuple[PermissionUpdate, ...],
) -> list[dict[str, object]]:
    result: list[dict[str, object]] = []
    for suggestion in suggestions:
        item = suggestion.as_dict()
        constraints = dict(item.get("constraints", {}))
        if "command" in constraints:
            constraints["command_sha256"] = hashlib.sha256(
                str(constraints.pop("command")).encode("utf-8")
            ).hexdigest()
        item["constraints"] = constraints
        result.append(item)
    return result


def _rule_digest(behavior: str, tool: str, constraints: dict[str, object], source: str) -> str:
    encoded = json.dumps([behavior, tool, constraints, source], sort_keys=True, default=str)
    return "rule_" + hashlib.sha256(encoded.encode()).hexdigest()[:20]


def _contains_glob(value: str) -> bool:
    return any(char in value for char in "*?[")


def _path_glob_match(path: str, pattern: str) -> bool:
    normalized_path = path.replace("\\", "/").removeprefix("./") or "."
    normalized_pattern = pattern.replace("\\", "/").removeprefix("./") or "."
    if PurePosixPath(normalized_pattern).is_absolute() or ".." in PurePosixPath(normalized_pattern).parts:
        return False
    regex = ""
    index = 0
    while index < len(normalized_pattern):
        char = normalized_pattern[index]
        if char == "*":
            if index + 1 < len(normalized_pattern) and normalized_pattern[index + 1] == "*":
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


def _allow_suggestions(tool: str, constraints: dict[str, object]) -> tuple[PermissionUpdate, ...]:
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


def _persist_file_update(path: Path, update: PermissionUpdate) -> str:
    if path.is_symlink():
        raise ValueError(f"permissions file must not be a symlink: {path}")
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    if path.exists():
        document = tomlkit.parse(path.read_text(encoding="utf-8"))
    else:
        if update.operation is PermissionUpdateOperation.REMOVE:
            raise ValueError("permission rule does not exist")
        document = tomlkit.document()
    document["permissions_version"] = PERMISSIONS_VERSION
    rules = document.get("rules")
    if not isinstance(rules, list):
        rules = tomlkit.aot()
        document["rules"] = rules
    rule_id = update.rule_id or f"rule_{uuid.uuid4().hex}"
    if update.operation is PermissionUpdateOperation.REMOVE:
        remaining = [item for item in rules if str(item.get("id", "")) != rule_id]
        if len(remaining) == len(rules):
            raise ValueError("permission rule does not exist")
        replacement = tomlkit.aot()
        for item in remaining:
            replacement.append(item)
        document["rules"] = replacement
    else:
        table = tomlkit.table()
        table["id"] = rule_id
        table["behavior"] = update.behavior.value
        table["tool"] = update.tool
        if update.constraints:
            constraints = tomlkit.table()
            for key, value in update.constraints.items():
                constraints[key] = value
            table["constraints"] = constraints
        rules.append(table)
    temporary = path.with_name(path.name + f".{uuid.uuid4().hex}.tmp")
    temporary.write_text(tomlkit.dumps(document), encoding="utf-8")
    temporary.chmod(0o600)
    temporary.replace(path)
    return rule_id


__all__ = [
    "PERMISSIONS_VERSION",
    "PermissionBehavior",
    "PermissionDecision",
    "PermissionDestination",
    "PermissionEngine",
    "PermissionMiddleware",
    "PermissionRule",
    "PermissionSpec",
    "PermissionUpdate",
    "PermissionUpdateOperation",
]
