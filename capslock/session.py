"""Compatibility facade over the versioned storage repositories."""

from __future__ import annotations

from pathlib import Path

from .domain import ChangeInfo, CommandInfo, ExternalActionInfo, SessionInfo, SourceInfo, TaskInfo
from .storage import ActionRepository, Database, RunRepository, SessionRepository, SettingsRepository, SkillSettingsRepository, SourceRepository, TaskRepository
from .storage.repositories import Repository


class SessionStore(SessionRepository, RunRepository, ActionRepository, TaskRepository, SettingsRepository, SourceRepository, SkillSettingsRepository):
    """Workspace persistence facade retained for the public runtime API."""

    def __init__(self, database: str | Path) -> None:
        self.database = Database(database)
        self.path = self.database.path
        Repository.__init__(self, self.database.connection)
        self._connection = self.database.connection

    def close(self) -> None:
        self.database.close()

    def __enter__(self) -> "SessionStore":
        return self

    def __exit__(self, exc_type: object, exc: object, traceback: object) -> None:
        self.close()


__all__ = [
    "ChangeInfo",
    "CommandInfo",
    "ExternalActionInfo",
    "SessionInfo",
    "SessionStore",
    "SourceInfo",
    "TaskInfo",
]
