"""Validate child Agent outputs before they enter a parent context."""

from __future__ import annotations

import hashlib
from typing import Any, Mapping

from .models import AgentTaskContract, AgentTaskState, ValidatedAgentOutput
from .workspace import WorkspaceSnapshot


class VerificationError(ValueError):
    pass


class AgentOutputVerifier:
    def verify(
        self,
        contract: AgentTaskContract,
        snapshot: WorkspaceSnapshot,
        output: Mapping[str, Any],
    ) -> ValidatedAgentOutput:
        if not isinstance(output, Mapping):
            raise VerificationError("child output must be an object")
        summary = output.get("summary", "")
        if not isinstance(summary, str) or not summary.strip():
            raise VerificationError("child output summary is required")
        if len(summary) > 20_000:
            raise VerificationError("child output summary is too large")
        self._validate_schema(contract.verification_requirements.output_schema, output)
        evidence = self._evidence(contract, snapshot, output.get("evidence", ()))
        artifacts = self._artifacts(contract, snapshot, output.get("artifacts", ()))
        checks = self._checks(contract, output.get("checks", ()))
        usage = self._usage(output.get("_usage", {}), output.get("_budget", {}))
        return ValidatedAgentOutput(
            task_id=contract.task_id,
            state=AgentTaskState.COMPLETED,
            summary=summary.strip(),
            evidence=tuple(evidence),
            artifacts=tuple(artifacts),
            checks=tuple(checks),
            usage=usage,
            verified=True,
        )

    def rejected(self, contract: AgentTaskContract, error: str) -> ValidatedAgentOutput:
        return ValidatedAgentOutput(
            task_id=contract.task_id,
            state=AgentTaskState.FAILED,
            summary="",
            verified=False,
            error=error,
        )

    def _validate_schema(
        self, schema: Mapping[str, Any], output: Mapping[str, Any]
    ) -> None:
        if not schema:
            return
        public = {
            name: value for name, value in output.items() if not name.startswith("_")
        }
        self._validate_schema_value(schema, public, "output")

    def _validate_schema_value(
        self, schema: Mapping[str, Any], value: Any, path: str
    ) -> None:
        expected = schema.get("type")
        types = {
            "object": lambda item: isinstance(item, Mapping),
            "array": lambda item: isinstance(item, (list, tuple)),
            "string": lambda item: isinstance(item, str),
            "integer": lambda item: (
                isinstance(item, int) and not isinstance(item, bool)
            ),
            "number": lambda item: (
                isinstance(item, (int, float)) and not isinstance(item, bool)
            ),
            "boolean": lambda item: isinstance(item, bool),
            "null": lambda item: item is None,
        }
        if expected is not None:
            if expected not in types:
                raise VerificationError(f"unsupported output schema type: {expected}")
            if not types[expected](value):
                raise VerificationError(f"{path} must be {expected}")
        if "enum" in schema:
            choices = schema["enum"]
            if not isinstance(choices, list) or value not in choices:
                raise VerificationError(f"{path} is not an allowed value")
        if isinstance(value, Mapping):
            required = schema.get("required", ())
            if not isinstance(required, list) or not all(
                isinstance(item, str) for item in required
            ):
                raise VerificationError("output schema required must be a string array")
            missing = [name for name in required if name not in value]
            if missing:
                raise VerificationError(
                    f"{path} is missing required fields: {', '.join(missing)}"
                )
            properties = schema.get("properties", {})
            if not isinstance(properties, Mapping):
                raise VerificationError("output schema properties must be an object")
            if schema.get("additionalProperties") is False:
                extra = sorted(set(value) - set(properties))
                if extra:
                    raise VerificationError(
                        f"{path} has undeclared fields: {', '.join(extra)}"
                    )
            for name, definition in properties.items():
                if name in value:
                    if not isinstance(definition, Mapping):
                        raise VerificationError(
                            f"output schema property {name} must be an object"
                        )
                    self._validate_schema_value(
                        definition, value[name], f"{path}.{name}"
                    )
        if isinstance(value, (list, tuple)):
            minimum, maximum = schema.get("minItems"), schema.get("maxItems")
            if isinstance(minimum, int) and len(value) < minimum:
                raise VerificationError(f"{path} has too few items")
            if isinstance(maximum, int) and len(value) > maximum:
                raise VerificationError(f"{path} has too many items")
            item_schema = schema.get("items")
            if item_schema is not None:
                if not isinstance(item_schema, Mapping):
                    raise VerificationError("output schema items must be an object")
                for index, item in enumerate(value):
                    self._validate_schema_value(item_schema, item, f"{path}[{index}]")

    def _records(self, value: Any, label: str) -> list[dict[str, Any]]:
        if value is None:
            return []
        if not isinstance(value, (list, tuple)) or any(
            not isinstance(item, Mapping) for item in value
        ):
            raise VerificationError(f"{label} must be a list of objects")
        return [dict(item) for item in value]

    def _artifacts(
        self, contract: AgentTaskContract, snapshot: WorkspaceSnapshot, value: Any
    ) -> list[dict[str, Any]]:
        records = self._records(value, "artifacts")
        requirements = contract.verification_requirements
        if len(records) > requirements.max_artifacts:
            raise VerificationError("too many child artifacts")
        expected_paths = set(requirements.required_paths)
        seen: set[str] = set()
        verified: list[dict[str, Any]] = []
        for item in records:
            path_value = item.get("path")
            if not isinstance(path_value, str) or path_value in seen:
                raise VerificationError("artifact paths must be unique strings")
            seen.add(path_value)
            path = snapshot.resolve(path_value, allowed_paths=contract.allowed_paths)
            if not path.is_file():
                raise VerificationError(f"artifact does not exist: {path_value}")
            size = path.stat().st_size
            if size > requirements.max_artifact_bytes:
                raise VerificationError(f"artifact exceeds size limit: {path_value}")
            digest = hashlib.sha256(path.read_bytes()).hexdigest()
            declared = item.get("sha256")
            if declared is not None and declared != digest:
                raise VerificationError(f"artifact digest mismatch: {path_value}")
            verified.append({"path": path_value, "sha256": digest, "bytes": size})
        if not expected_paths.issubset(seen):
            missing = sorted(expected_paths - seen)
            raise VerificationError(
                f"required artifacts are missing: {', '.join(missing)}"
            )
        return verified

    def _evidence(
        self, contract: AgentTaskContract, snapshot: WorkspaceSnapshot, value: Any
    ) -> list[dict[str, Any]]:
        records = self._records(value, "evidence")
        verified: list[dict[str, Any]] = []
        for item in records:
            path_value = item.get("path")
            if not isinstance(path_value, str):
                raise VerificationError("evidence path must be a string")
            path = snapshot.resolve(path_value, allowed_paths=contract.allowed_paths)
            if not path.is_file():
                raise VerificationError(f"evidence does not exist: {path_value}")
            digest = hashlib.sha256(path.read_bytes()).hexdigest()
            declared = item.get("sha256")
            if declared is not None and declared != digest:
                raise VerificationError(f"evidence digest mismatch: {path_value}")
            verified.append({**item, "path": path_value, "sha256": digest})
        return verified

    def _checks(self, contract: AgentTaskContract, value: Any) -> list[dict[str, Any]]:
        records = self._records(value, "checks")
        statuses = {str(item.get("name")): str(item.get("status")) for item in records}
        missing = [
            name
            for name in contract.verification_requirements.required_checks
            if name not in statuses
        ]
        if missing:
            raise VerificationError(
                f"required checks are missing: {', '.join(missing)}"
            )
        failed = [
            name
            for name in contract.verification_requirements.required_checks
            if statuses.get(name) != "passed"
        ]
        if failed:
            raise VerificationError(f"required checks failed: {', '.join(failed)}")
        return records

    def _usage(self, usage: Any, budget: Any) -> dict[str, int | float]:
        usage = usage if isinstance(usage, Mapping) else {}
        budget = budget if isinstance(budget, Mapping) else {}
        used = budget.get("used", {})
        used = used if isinstance(used, Mapping) else {}
        result: dict[str, int | float] = {}
        for name in ("input_tokens", "output_tokens"):
            value = usage.get(name, 0)
            if not isinstance(value, int) or isinstance(value, bool) or value < 0:
                raise VerificationError(f"child usage {name} is invalid")
            result[name] = value
        cost = usage.get("cost_usd", 0)
        if not isinstance(cost, (int, float)) or isinstance(cost, bool) or cost < 0:
            raise VerificationError("child usage cost_usd is invalid")
        result["cost_usd"] = float(cost)
        for name in ("tool_rounds", "tool_calls"):
            value = used.get(name, 0)
            if not isinstance(value, int) or isinstance(value, bool) or value < 0:
                raise VerificationError(f"child budget {name} is invalid")
            result[name] = value
        return result
