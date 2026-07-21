"""Explicit configuration validation, migration, and resolution stages."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Any

from .document import DocumentReader


class DocumentValidator:
    def __init__(
        self, validate: Callable[[dict[str, object]], tuple[Any, ...]]
    ) -> None:
        self.validate_document = validate

    def validate(self, document: dict[str, object]) -> tuple[Any, ...]:
        return self.validate_document(document)


class DocumentMigrator:
    def __init__(self, migrate: Callable[..., tuple[bool, str | None]]) -> None:
        self.migrate_document = migrate

    def migrate(self, path: Path, *, apply: bool) -> tuple[bool, str | None]:
        return self.migrate_document(path, apply=apply)


class SettingsResolver:
    def __init__(self, resolve: Callable[..., Any]) -> None:
        self.resolve_settings = resolve

    def resolve(self, workspace: Path, **kwargs: Any) -> Any:
        return self.resolve_settings(workspace, **kwargs)


class ConfigurationPipeline:
    def __init__(
        self,
        *,
        validator: DocumentValidator,
        migrator: DocumentMigrator,
        reader: DocumentReader | None = None,
    ) -> None:
        self.reader = reader or DocumentReader()
        self.validator = validator
        self.migrator = migrator

    def load(
        self, path: Path, *, migrate: bool, current_version: int
    ) -> dict[str, object]:
        document = self.reader.read(path)
        if document.get("config_version", 0) < current_version and migrate:
            self.migrator.migrate(path, apply=True)
            document = self.reader.read(path)
        issues = self.validator.validate(document)
        errors = [item for item in issues if item.severity == "error"]
        if errors:
            first = errors[0]
            raise ValueError(f"invalid config at {first.path}: {first.message}")
        return document
