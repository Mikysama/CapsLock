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


class CompiledSchema:
    def __init__(self, schema: dict[str, object]) -> None:
        try:
            Draft202012Validator.check_schema(schema)
        except SchemaError as exc:
            raise SchemaValidationError(f"invalid tool schema: {exc.message}") from exc
        self.validator = Draft202012Validator(schema)

    def validate(self, value: object) -> None:
        errors = sorted(self.validator.iter_errors(value), key=lambda item: list(item.path))
        if not errors:
            return
        error = errors[0]
        path = "$" + "".join(
            f"[{item}]" if isinstance(item, int) else f".{item}" for item in error.path
        )
        raise SchemaValidationError(f"{path}: {error.message}")


@lru_cache(maxsize=512)
def _compile(encoded: str) -> CompiledSchema:
    return CompiledSchema(json.loads(encoded))


def compile_json_schema(schema: dict[str, object]) -> CompiledSchema:
    if not isinstance(schema, dict):
        raise SchemaValidationError("$: schema must be an object")
    return _compile(json.dumps(schema, sort_keys=True, separators=(",", ":")))


def validate_json_schema(value: object, schema: dict[str, object], path: str = "$") -> None:
    del path
    compile_json_schema(schema).validate(value)
