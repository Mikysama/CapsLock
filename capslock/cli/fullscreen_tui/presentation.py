"""Compatibility exports for the shared terminal presentation model."""

from ..presentation import (
    ActionPresentation,
    present_action,
    present_permission_request,
    truncate_preview,
)

__all__ = [
    "ActionPresentation",
    "present_action",
    "present_permission_request",
    "truncate_preview",
]
