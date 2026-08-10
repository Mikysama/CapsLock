"""Compiled JSON Schema 2020-12 validation for tool boundaries."""

from __future__ import annotations

from functools import lru_cache
import json

try:
    from jsonschema import Draft202012Validator
    from jsonschema.exceptions import SchemaError
except ImportError as exc:  # pragma: no cover - dependency installation error
    raise RuntimeError("CapsLock requires the jsonschema package") from exc


class SchemaValidationError(ValueError):
    code = "invalid_tool_arguments"

    def __init__(
        self,
        message: str,
        *,
        path: str = "$",
        expected: str | None = None,
        received_type: str | None = None,
    ) -> None:
        super().__init__(message)
        self.path = path
        self.expected = expected
        self.received_type = received_type

    def detail(self) -> dict[str, object]:
        return {
            "path": self.path,
            "expected": self.expected,
            "received_type": self.received_type,
            "retryable": True,
            "suggested_tools": [],
            "repair_attempt": 0,
        }


class CompiledSchema:
    def __init__(self, schema: dict[str, object]) -> None:
        try:
            Draft202012Validator.check_schema(schema)
        except SchemaError as exc:
            raise SchemaValidationError(f"invalid tool schema: {exc.message}") from exc
        self.validator = Draft202012Validator(schema)

    def validate(self, value: object) -> None:
        errors = sorted(
            self.validator.iter_errors(value), key=lambda item: list(item.path)
        )
        if not errors:
            return
        error = errors[0]
        path = "$" + "".join(
            f"[{item}]" if isinstance(item, int) else f".{item}" for item in error.path
        )
        expected = f"{error.validator}={json.dumps(error.validator_value, default=str)}"
        raise SchemaValidationError(
            f"{path}: {error.message}",
            path=path,
            expected=expected,
            received_type=type(error.instance).__name__,
        )


@lru_cache(maxsize=512)
def _compile(encoded: str) -> CompiledSchema:
    return CompiledSchema(json.loads(encoded))


def compile_json_schema(schema: dict[str, object]) -> CompiledSchema:
    if not isinstance(schema, dict):
        raise SchemaValidationError("$: schema must be an object")
    return _compile(json.dumps(schema, sort_keys=True, separators=(",", ":")))


def validate_json_schema(
    value: object, schema: dict[str, object], path: str = "$"
) -> None:
    del path
    compile_json_schema(schema).validate(value)


def strip_optional_nulls(value: object, schema: dict[str, object]) -> object:
    """Remove provider strict-mode null placeholders for optional properties."""
    if isinstance(value, list) and schema.get("type") == "array":
        items = schema.get("items")
        if isinstance(items, dict):
            return [strip_optional_nulls(item, items) for item in value]
        return value
    if not isinstance(value, dict) or schema.get("type") != "object":
        return value
    properties = schema.get("properties", {})
    if not isinstance(properties, dict):
        return value
    required = set(schema.get("required", ()))
    output: dict[str, object] = {}
    for name, item in value.items():
        child = properties.get(name)
        if item is None and name not in required:
            continue
        output[name] = (
            strip_optional_nulls(item, child) if isinstance(child, dict) else item
        )
    return output
