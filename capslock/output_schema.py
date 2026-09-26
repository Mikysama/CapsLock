"""Local-only validation for automation output contracts."""

from __future__ import annotations

import json
from pathlib import Path

from jsonschema import Draft202012Validator
from jsonschema.exceptions import ValidationError
from referencing import Registry, Resource
from referencing.exceptions import Unresolvable
from referencing.jsonschema import DRAFT202012


class OutputSchemaError(ValueError):
    code = "output_schema_validation_failed"


def validate_output_schema(schema: dict[str, object]) -> None:
    references: list[str] = []

    def inspect(value: object) -> None:
        if isinstance(value, dict):
            for key, child in value.items():
                if key in {"$ref", "$dynamicRef", "$recursiveRef"} and (
                    not isinstance(child, str) or not child.startswith("#")
                ):
                    raise ValueError("output schema references must be local fragments")
                if key in {"$ref", "$dynamicRef", "$recursiveRef"}:
                    references.append(child)
                if key == "$id":
                    raise ValueError(
                        "output schema reference base changes are unsupported"
                    )
                inspect(child)
        elif isinstance(value, list):
            for child in value:
                inspect(child)

    inspect(schema)
    Draft202012Validator.check_schema(schema)
    resolver = Registry().resolver_with_root(
        Resource.from_contents(schema, default_specification=DRAFT202012)
    )
    for reference in references:
        try:
            resolver.lookup(reference)
        except Unresolvable as exc:
            raise ValueError(
                f"unresolved output schema reference: {reference}"
            ) from exc


def _reject_non_json_constant(value: str) -> None:
    raise ValueError(f"non-JSON numeric constant: {value}")


def load_output_schema(path: Path) -> dict[str, object]:
    if path.stat().st_size > 1_048_576:
        raise ValueError("output schema exceeds 1 MiB")
    schema = json.loads(
        path.read_text(encoding="utf-8"), parse_constant=_reject_non_json_constant
    )
    if not isinstance(schema, dict):
        raise ValueError("output schema must be an object")
    validate_output_schema(schema)
    return {
        "type": "json_schema",
        "json_schema": {"name": "capslock_result", "strict": True, "schema": schema},
    }


def validate_output(text: str, response_format: dict[str, object]) -> object:
    from .structured_output import response_schema

    try:
        schema = response_schema(response_format)
        validate_output_schema(schema)
        value = json.loads(text, parse_constant=_reject_non_json_constant)
        Draft202012Validator(schema).validate(value)
    except (ValueError, ValidationError, Unresolvable, RecursionError) as exc:
        raise OutputSchemaError(
            "structured output does not match the requested schema"
        ) from exc
    return value
