"""Atomic, explicit workspace-local permission presets."""

from __future__ import annotations

import os
import tempfile
from pathlib import Path

import tomlkit

EDIT_TOOLS = ("create_file", "edit_file", "write_file", "edit_notebook")
PREFIX = "preset_workspace_edit_"


def write_workspace_edit_preset(path: Path, *, enabled: bool) -> None:
    if any(component.is_symlink() for component in (path, *path.parents)):
        raise ValueError("permissions file must not be a symlink")
    document = (
        tomlkit.parse(path.read_text(encoding="utf-8"))
        if path.exists()
        else tomlkit.document()
    )
    if document.get("permissions_version", 2) != 2:
        raise ValueError("upgrade permission rules before applying a preset")
    rules = document.get("rules", [])
    if not isinstance(rules, list):
        raise ValueError("invalid permission rules")
    replacement = tomlkit.aot()
    for item in rules:
        if not isinstance(item, dict):
            raise ValueError("invalid permission rule")
        identifier = str(item.get("id", ""))
        if identifier in {PREFIX + tool for tool in EDIT_TOOLS}:
            tool = identifier.removeprefix(PREFIX)
            if (
                item.get("behavior") != "allow"
                or item.get("tool") != tool
                or dict(item.get("constraints", {})) != {"path": "**"}
            ):
                raise ValueError(f"permission preset rule ID conflict: {identifier}")
        else:
            replacement.append(item)
    if enabled:
        for tool in EDIT_TOOLS:
            rule = tomlkit.table()
            rule.update({"id": PREFIX + tool, "behavior": "allow", "tool": tool})
            rule["constraints"] = {"path": "**"}
            replacement.append(rule)
    document["permissions_version"] = 2
    document["rules"] = replacement
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd, name = tempfile.mkstemp(prefix=path.name + ".", dir=path.parent)
    temporary = Path(name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(tomlkit.dumps(document))
            handle.flush()
            os.fsync(handle.fileno())
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)
