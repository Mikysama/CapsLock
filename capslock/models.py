"""User-selectable chat models."""

from __future__ import annotations


SELECTABLE_MODELS = (
    "deepseek-v4-flash",
    "deepseek-v4-pro",
)


def selectable_model(value: str) -> str:
    """Return a normalized selectable model or raise a user-facing error."""

    normalized = value.strip().casefold()
    if normalized not in SELECTABLE_MODELS:
        choices = " or ".join(SELECTABLE_MODELS)
        raise ValueError(f"model must be {choices}")
    return normalized
