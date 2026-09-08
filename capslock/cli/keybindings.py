"""User keybinding configuration for the inline composer."""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class KeybindingSettings:
    bindings: dict[str, tuple[str, ...]]
    vim_mode: bool = False


DEFAULT_BINDINGS = {
    "insert_newline": ("c-j",),
    "submit": ("c-m",),
    "cancel": ("c-c",),
    "toggle_details": ("c-o",),
}


def load_keybindings(path: Path | None) -> KeybindingSettings:
    if path is None or not path.is_file():
        return KeybindingSettings(dict(DEFAULT_BINDINGS))
    if path.is_symlink():
        raise ValueError("keybinding configuration must not be a symlink")
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError("keybinding configuration must be an object")
    raw = payload.get("bindings", {})
    if not isinstance(raw, dict):
        raise ValueError("keybinding bindings must be an object")
    bindings = dict(DEFAULT_BINDINGS)
    for action, value in raw.items():
        if action not in DEFAULT_BINDINGS:
            raise ValueError(f"unsupported keybinding action: {action}")
        values = (
            (value,)
            if isinstance(value, str)
            else tuple(value)
            if isinstance(value, list)
            else ()
        )
        if not values or not all(isinstance(item, str) and item for item in values):
            raise ValueError(f"invalid keybinding for {action}")
        bindings[action] = values
    return KeybindingSettings(bindings, bool(payload.get("vim_mode", False)))


__all__ = ["DEFAULT_BINDINGS", "KeybindingSettings", "load_keybindings"]
