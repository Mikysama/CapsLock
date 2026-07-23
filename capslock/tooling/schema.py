"""Small deterministic JSON Schema validator for tool boundaries.

CapsLock intentionally supports the JSON Schema subset accepted by model tool
declarations.  Keeping validation in-process avoids provider-specific behavior
and gives plugins and built-in tools the same failure contract.
"""

from __future__ import annotations

class SchemaValidationError(ValueError):
    code = "invalid_tool_arguments"


def validate_json_schema(value: object, schema: dict[str, object], path: str = "$") -> None:
    if not isinstance(schema, dict):
        raise SchemaValidationError(f"{path}: schema must be an object")
    expected = schema.get("type")
    if expected is not None:
        allowed = (expected,) if isinstance(expected, str) else tuple(expected)
        if not _matches(value, allowed):
            raise SchemaValidationError(
                f"{path}: expected {' or '.join(str(item) for item in allowed)}"
            )
    if "enum" in schema and value not in schema["enum"]:
        raise SchemaValidationError(f"{path}: value is not in the allowed enum")
    if isinstance(value, dict):
        properties = schema.get("properties", {})
        properties = properties if isinstance(properties, dict) else {}
        required = schema.get("required", [])
        if not isinstance(required, list):
            raise SchemaValidationError(f"{path}: schema required must be an array")
        missing = [name for name in required if name not in value]
        if missing:
            raise SchemaValidationError(f"{path}: missing required field {missing[0]}")
        if schema.get("additionalProperties") is False:
            unknown = sorted(set(value) - set(properties))
            if unknown:
                raise SchemaValidationError(f"{path}: unknown field {unknown[0]}")
        for name, item in value.items():
            child = properties.get(name)
            if isinstance(child, dict):
                validate_json_schema(item, child, f"{path}.{name}")
    if isinstance(value, list):
        minimum = schema.get("minItems")
        maximum = schema.get("maxItems")
        if isinstance(minimum, int) and len(value) < minimum:
            raise SchemaValidationError(f"{path}: contains too few items")
        if isinstance(maximum, int) and len(value) > maximum:
            raise SchemaValidationError(f"{path}: contains too many items")
        items = schema.get("items")
        if isinstance(items, dict):
            for index, item in enumerate(value):
                validate_json_schema(item, items, f"{path}[{index}]")
    if isinstance(value, str):
        minimum = schema.get("minLength")
        maximum = schema.get("maxLength")
        if isinstance(minimum, int) and len(value) < minimum:
            raise SchemaValidationError(f"{path}: string is too short")
        if isinstance(maximum, int) and len(value) > maximum:
            raise SchemaValidationError(f"{path}: string is too long")
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        minimum = schema.get("minimum")
        maximum = schema.get("maximum")
        if isinstance(minimum, (int, float)) and value < minimum:
            raise SchemaValidationError(f"{path}: value is below minimum")
        if isinstance(maximum, (int, float)) and value > maximum:
            raise SchemaValidationError(f"{path}: value is above maximum")


def _matches(value: object, allowed: tuple[object, ...]) -> bool:
    checks = {
        "object": lambda: isinstance(value, dict),
        "array": lambda: isinstance(value, list),
        "string": lambda: isinstance(value, str),
        "integer": lambda: isinstance(value, int) and not isinstance(value, bool),
        "number": lambda: isinstance(value, (int, float)) and not isinstance(value, bool),
        "boolean": lambda: isinstance(value, bool),
        "null": lambda: value is None,
    }
    return any(item in checks and checks[item]() for item in allowed)
