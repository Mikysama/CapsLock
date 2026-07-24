"""Shared compact JSON Schema constructors for built-in tools."""


def _str() -> dict[str, object]:
    return {"type": "string"}


def _int() -> dict[str, object]:
    return {"type": "integer"}


def _schema(
    properties: dict[str, object], required: list[str] | None = None
) -> dict[str, object]:
    schema: dict[str, object] = {
        "type": "object",
        "properties": properties,
        "additionalProperties": False,
    }
    if required:
        schema["required"] = required
    return schema
