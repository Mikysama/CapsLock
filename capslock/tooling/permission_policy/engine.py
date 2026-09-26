"""Permission rule coordination, persistence, and trust boundaries."""

from __future__ import annotations

from ...permissions import permission_arguments_digest

import hashlib
import json
import tomllib
import uuid
from pathlib import Path, PurePosixPath
from types import SimpleNamespace
from typing import Any, Iterable

import tomlkit

from ...permissions import PermissionMode
from .decision import PermissionDecisionEngine
from .models import (
    PERMISSIONS_VERSION,
    PermissionBehavior,
    PermissionDecision,
    PermissionDestination,
    PermissionRule,
    PermissionUpdate,
    PermissionUpdateOperation,
)
from .ports import PermissionSpec, PermissionStorePort
from .specs import (
    DefaultPermissionSpec,
    FilePermissionSpec,
    McpPermissionSpec,
    ShellPermissionSpec,
    WebPermissionSpec,
)
from ..contracts import (
    ExecutionContext,
    ResolvedToolPolicy,
    ToolDefinition,
)


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


class PermissionEngine:
    """Merge immutable boundaries, v2 file rules, and session grants."""

    def __init__(
        self, paths: Iterable[tuple[str, Path]], repository: PermissionStorePort | Any
    ) -> None:
        self.paths = tuple(paths)
        self.repository = repository
        self._diagnostics: list[str] = []
        self._decision_engine = PermissionDecisionEngine()

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

    @staticmethod
    def operation_identity(
        name: str, arguments: dict[str, Any]
    ) -> tuple[str, dict[str, Any]]:
        # Keep grants scoped to the operation they authorized before consolidation.
        from ..tools.collaboration import resolve_agent_operation

        return resolve_agent_operation(name, arguments)

    def _matching_rules(self, rules, tool, arguments):
        operation, routed = self.operation_identity(tool.name, arguments)
        original = SimpleNamespace(name=operation)
        spec = self.spec_for(original)
        matching = [rule for rule in rules if spec.matches(rule, original, routed)]
        related = []
        if operation != tool.name:
            related.append(tool.name)
        if tool.name == "write_file" and arguments.get("expected_sha256") is None:
            related.append("create_file")
        if tool.name == "list_tasks" and arguments.get("task_id") is not None:
            related.append("get_task")
        # Restrictions on either spelling remain effective. An old allow on the
        # broader public name cannot authorize a newly added Agent operation.
        for name in related:
            proxy = SimpleNamespace(name=name)
            matching.extend(
                rule
                for rule in rules
                if rule.behavior is not PermissionBehavior.ALLOW
                and self.spec_for(proxy).matches(rule, proxy, arguments)
                and rule not in matching
            )
        return matching

    async def decide(
        self,
        tool: ToolDefinition,
        arguments: dict[str, Any],
        policy: ResolvedToolPolicy,
        context: ExecutionContext,
    ) -> PermissionDecision:
        spec = self.spec_for(tool)
        normalized = spec.normalize(tool, arguments, context)
        digest = permission_arguments_digest(normalized)
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
        matching = self._matching_rules(rules, tool, normalized)
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
        selected = self._decision_engine.select_rule(matching)
        if selected is not None:
            behavior = selected.behavior
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

        deterministic_shell_allow = (
            context.permission_mode is PermissionMode.APPROVE_FOR_ME
            and tool.name == "shell"
            and context.runtime_state.get("shell_deterministic_behavior") == "allow"
            and _deterministic_shell_allow(normalized)
            and normalized.get("sandbox") == "default"
            and normalized.get("network") == []
            and normalized.get("background", False) is False
        )
        default = self._decision_engine.default_behavior(
            mode=context.permission_mode,
            policy=policy,
            deterministic_shell_allow=deterministic_shell_allow,
            shell_tool=tool.name == "shell",
        )
        operation, routed = self.operation_identity(tool.name, normalized)
        operation_tool = SimpleNamespace(name=operation)
        return PermissionDecision(
            default,
            "permission_mode",
            f"defaulted from permission mode {context.permission_mode.value}",
            "mode_default",
            context.permission_mode,
            normalized_arguments_sha256=digest,
            suggestions=self.spec_for(operation_tool).suggest_updates(
                operation_tool,
                routed,
            )
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
        if (
            any(
                rule.source == "project" and rule.behavior is PermissionBehavior.ALLOW
                for rule in rules
            )
            and not await self._project_allow_is_trusted()
        ):
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
                if not update.rule_id or not hasattr(
                    self.repository, "remove_session_permission_rule"
                ):
                    raise ValueError("session rule removal is unavailable")
                await self.repository.remove_session_permission_rule(
                    session_id, update.rule_id
                )
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
            raise ValueError(
                f"permission destination is unavailable: {update.destination.value}"
            )
        return _persist_file_update(path, update)

    async def verify_explicit_allow(
        self,
        *,
        session_id: str,
        tool: str,
        arguments: dict[str, Any],
    ) -> bool:
        proxy = type("PermissionToolProxy", (), {"name": tool})()
        matching = self._matching_rules(await self.rules(session_id), proxy, arguments)
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
        await self.repository.set_permission_setting(
            "project_permissions_sha256", digest
        )
        return digest

    async def _project_allow_is_trusted(self) -> bool:
        digest = self._project_digest()
        if digest is None or not hasattr(self.repository, "permission_setting"):
            return False
        return (
            await self.repository.permission_setting("project_permissions_sha256")
            == digest
        )

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
                self._diagnostics.append(
                    f"invalid {source} permissions file {path}: {exc}"
                )
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
                self._diagnostics.append(
                    f"unsupported permissions version in {path}: {version}"
                )
                yield PermissionRule(
                    PermissionBehavior.ASK, "*", {}, source, matcher_version=1
                )
                continue
            raw_rules = document.get("rules", document.get("rule", []))
            if not isinstance(raw_rules, list):
                self._diagnostics.append(
                    f"permissions rules must be an array in {path}"
                )
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
                    self._diagnostics.append(
                        f"invalid rule {index + 1} in {path}: {exc}"
                    )
                    yield PermissionRule(
                        PermissionBehavior.ASK,
                        "*",
                        {},
                        source,
                        matcher_version=1,
                        diagnostic="invalid rule; all automatic grants are disabled",
                    )
                    continue
                if (
                    version == 1
                    and rule.behavior is PermissionBehavior.ALLOW
                    and _old_allow_is_ambiguous(rule)
                ):
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
    identifier = (
        str(raw["id"])
        if raw.get("id") is not None
        else _rule_digest(behavior.value, tool, constraints, source)
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
            if (
                "\x00" in text
                or PurePosixPath(text).is_absolute()
                or ".." in PurePosixPath(text).parts
            ):
                raise ValueError(f"unsafe {key} constraint")
            if "[" in text or "]" in text:
                raise ValueError(f"unsupported or invalid {key} glob; use *, **, or ?")
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


def _rule_digest(
    behavior: str, tool: str, constraints: dict[str, object], source: str
) -> str:
    encoded = json.dumps(
        [behavior, tool, constraints, source], sort_keys=True, default=str
    )
    return "rule_" + hashlib.sha256(encoded.encode()).hexdigest()[:20]


def _contains_glob(value: str) -> bool:
    return any(char in value for char in "*?[")


def _deterministic_shell_allow(arguments: dict[str, Any]) -> bool:
    from ...shell import assess_shell

    return assess_shell(str(arguments.get("command", ""))).behavior == "allow"


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


__all__ = ["PermissionEngine"]
