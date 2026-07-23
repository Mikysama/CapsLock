"""Versioned and atomic TOML configuration migration."""

from __future__ import annotations

import os
import shutil
import tempfile
from datetime import UTC, datetime
from pathlib import Path

from .document import DocumentReader
from .validation import CONFIG_VERSION, validate_config_document


def migrate_config(path: Path, *, apply: bool) -> tuple[bool, str | None]:
    document = DocumentReader().read(path)
    version = document.get("config_version", 0)
    if version == CONFIG_VERSION:
        return False, None
    if version not in {0, 1}:
        raise ValueError(f"unsupported configuration version {version}")
    runtime = document.get("runtime")
    if isinstance(runtime, dict) and "max_turns" in runtime:
        raise ValueError(
            "runtime.max_turns was removed in 2.0.0; use runtime.max_tool_rounds"
        )
    plaintext = [
        item
        for item in validate_config_document(document)
        if item.code == "plaintext_credential"
    ]
    if plaintext:
        raise ValueError(plaintext[0].message)
    try:
        import tomlkit
    except ImportError as exc:  # pragma: no cover
        raise RuntimeError("tomlkit is required for configuration migration") from exc
    parsed = tomlkit.parse(path.read_text(encoding="utf-8"))
    parsed["config_version"] = CONFIG_VERSION
    providers = parsed.get("providers")
    if providers is not None:
        for provider in providers.values():
            if "credential" not in provider and "api_key_env" in provider:
                provider["credential"] = f"env:{provider['api_key_env']}"
                del provider["api_key_env"]
    rendered = tomlkit.dumps(parsed)
    if not apply:
        return True, rendered
    backup_dir = path.parent / "state" / "backups"
    backup_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%S%fZ")
    backup = backup_dir / f"config.v{version}.{stamp}.toml"
    shutil.copy2(path, backup)
    atomic_write(path, rendered)
    return True, str(backup)


def atomic_write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary: str | None = None
    try:
        with tempfile.NamedTemporaryFile(
            "w", encoding="utf-8", dir=path.parent, prefix=".config-", delete=False
        ) as handle:
            temporary = handle.name
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        if temporary and Path(temporary).exists():
            Path(temporary).unlink()
