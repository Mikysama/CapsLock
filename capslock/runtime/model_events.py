"""Provider terminal metadata normalization independent of model transport."""

from __future__ import annotations

from typing import Any

from ..domain import ModelErrorCode, ModelRoutingError


def protocol_error(code: ModelErrorCode, message: str) -> ModelRoutingError:
    error = ModelRoutingError(message)
    error.code = code
    return error


def validate_completion(
    status: str | None, reason: str | None = None, error: str | None = None
) -> None:
    if status is None or status == "completed":
        return
    if status == "incomplete":
        raise protocol_error(
            ModelErrorCode.INCOMPLETE,
            f"model response incomplete: {reason or 'unknown reason'}",
        )
    raise protocol_error(
        ModelErrorCode.RESPONSE_FAILED, error or f"model response failed: {status}"
    )


def _error_message(error: Any) -> str | None:
    return _optional_string(
        error.get("message")
        if isinstance(error, dict)
        else getattr(error, "message", None)
    )


def _optional_string(value: object) -> str | None:
    return str(value) if value is not None and str(value) else None


def _incomplete_reason(response: Any) -> str | None:
    details = getattr(response, "incomplete_details", None)
    if isinstance(details, dict):
        return _optional_string(details.get("reason"))
    return _optional_string(getattr(details, "reason", None))
