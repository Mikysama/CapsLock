"""Create and validate private child-Agent workspace snapshots."""

from __future__ import annotations

import shutil
import tempfile
import hashlib
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ..policy import PolicyError, WorkspacePolicy
from ..workspace_writes import WorkspaceMutationCoordinator


_PRIVATE_NAMES = {
    ".netrc",
    ".npmrc",
    ".pypirc",
    "credentials",
    "credentials.json",
    "id_ed25519",
    "id_rsa",
    "secrets.json",
}
_EXCLUDED_NAMES = {
    ".git",
    ".capslock",
    ".venv",
    ".pytest_cache",
    ".ruff_cache",
    "__pycache__",
    "build",
    "dist",
    "node_modules",
}


def _is_private_name(name: str) -> bool:
    environment_file = (name == ".env" or name.startswith(".env.")) and name != (
        ".env.example"
    )
    return (
        name in _PRIVATE_NAMES
        or environment_file
        or Path(name).suffix.casefold() in {".key", ".pem"}
    )


@dataclass(frozen=True)
class WorkspaceSnapshot:
    source: Path
    root: Path

    def policy(self) -> WorkspacePolicy:
        return WorkspacePolicy(self.root)

    def resolve(self, requested: str, *, allowed_paths: tuple[str, ...] = ()) -> Path:
        path = self.policy().resolve(requested)
        relative = path.relative_to(self.root)
        allowed = [Path(item) for item in allowed_paths]
        if not any(relative == item or item in relative.parents for item in allowed):
            raise PolicyError("path is outside the child task allowlist")
        return path


class ScopedWorkspacePolicy(WorkspacePolicy):
    """WorkspacePolicy narrowed to the paths granted by one child contract."""

    def __init__(self, root: Path, allowed_paths: tuple[str, ...] = ()) -> None:
        super().__init__(root)
        object.__setattr__(
            self, "allowed_paths", tuple(Path(item) for item in allowed_paths)
        )

    def resolve(self, requested_path: str = ".") -> Path:
        path = super().resolve(requested_path)
        allowed = getattr(self, "allowed_paths", ())
        relative = path.relative_to(self.root)
        if not any(relative == item or item in relative.parents for item in allowed):
            raise PolicyError("path is outside the child task allowlist")
        return path


class AgentWorkspaceManager:
    """Own child snapshots and never follows source symlinks."""

    def __init__(
        self,
        parent_workspace: Path,
        *,
        state_root: Path | None = None,
        max_files: int = 10_000,
        max_bytes: int = 100_000_000,
        write_coordinator: WorkspaceMutationCoordinator | None = None,
    ) -> None:
        self.parent_workspace = parent_workspace.resolve()
        self.state_root = (
            state_root or self.parent_workspace / ".capslock" / "state" / "agents"
        ).resolve()
        if self.state_root == self.parent_workspace:
            raise ValueError("agent state root must not be the parent workspace")
        self.max_files = max_files
        self.max_bytes = max_bytes
        self.write_coordinator = write_coordinator or WorkspaceMutationCoordinator()
        self._baselines: dict[Path, dict[str, str]] = {}

    def create(self, task_id: str) -> WorkspaceSnapshot:
        if not task_id or Path(task_id).name != task_id:
            raise ValueError("invalid task id")
        self.state_root.mkdir(parents=True, exist_ok=True)
        target = Path(tempfile.mkdtemp(prefix=f"{task_id}-", dir=self.state_root))
        try:
            manifest: dict[str, str] = {}
            self._copy_tree(self.parent_workspace, target, [0, 0], manifest)
            self._baselines[target.resolve()] = manifest
        except BaseException:
            shutil.rmtree(target, ignore_errors=True)
            raise
        return WorkspaceSnapshot(self.parent_workspace, target)

    def publish_artifacts(
        self,
        snapshot: WorkspaceSnapshot,
        artifacts: tuple[dict[str, Any], ...],
        *,
        allowed_paths: tuple[str, ...],
    ) -> None:
        """Copy verified artifacts back without overwriting concurrent parent edits."""
        baseline = self._baselines.get(snapshot.root.resolve())
        if baseline is None:
            raise ValueError("child snapshot baseline is unavailable")
        parent_policy = WorkspacePolicy(self.parent_workspace)
        stage_root = Path(tempfile.mkdtemp(prefix=".publish-", dir=self.state_root))
        staged: list[tuple[Path, Path, str | None, str]] = []
        publish_temporaries: list[Path] = []
        backups: dict[Path, Path] = {}
        replaced: list[Path] = []
        retained_backups: set[Path] = set()
        created_directories: list[Path] = []
        try:
            seen: set[str] = set()
            for index, artifact in enumerate(artifacts):
                relative = str(artifact["path"])
                source = snapshot.resolve(relative, allowed_paths=allowed_paths)
                relative = source.relative_to(snapshot.root).as_posix()
                if relative in seen:
                    raise ValueError(f"duplicate artifact path: {relative}")
                seen.add(relative)
                target = parent_policy.resolve(relative)
                digest = hashlib.sha256(source.read_bytes()).hexdigest()
                if digest != artifact.get("sha256"):
                    raise ValueError(f"artifact changed after verification: {relative}")
                original_digest = baseline.get(relative)
                temporary = stage_root / str(index)
                shutil.copy2(source, temporary)
                if hashlib.sha256(temporary.read_bytes()).hexdigest() != digest:
                    raise ValueError(f"artifact changed while staging: {relative}")
                staged.append((temporary, target, original_digest, digest))
            with self.write_coordinator.lock(self.parent_workspace):
                prepared: list[tuple[Path, Path, str | None, str]] = []
                try:
                    for temporary, target, original_digest, digest in staged:
                        resolved = parent_policy.resolve(
                            str(target.relative_to(self.parent_workspace))
                        )
                        if resolved != target:
                            raise ValueError(
                                f"parent artifact path changed while publishing: {target}"
                            )
                        created_directories.extend(
                            self._create_parent_directories(target.parent)
                        )
                        handle, raw = tempfile.mkstemp(
                            prefix=f".{target.name}.",
                            suffix=".capslock-agent",
                            dir=target.parent,
                        )
                        os.close(handle)
                        publish_temporary = Path(raw)
                        publish_temporaries.append(publish_temporary)
                        shutil.copy2(temporary, publish_temporary)
                        if (
                            hashlib.sha256(publish_temporary.read_bytes()).hexdigest()
                            != digest
                        ):
                            raise ValueError(
                                f"artifact changed while preparing publish: {target}"
                            )
                        prepared.append(
                            (publish_temporary, target, original_digest, digest)
                        )
                    for _temporary, target, _original_digest, _digest in prepared:
                        resolved = parent_policy.resolve(
                            str(target.relative_to(self.parent_workspace))
                        )
                        if resolved != target:
                            raise ValueError(
                                f"parent artifact path changed while publishing: {target}"
                            )
                    self._revalidate_parent_artifacts(prepared)
                    for _temporary, target, original_digest, _digest in prepared:
                        if original_digest is None:
                            continue
                        handle, raw = tempfile.mkstemp(
                            prefix=f".{target.name}.",
                            suffix=".capslock-backup",
                            dir=target.parent,
                        )
                        os.close(handle)
                        backup = Path(raw)
                        backups[target] = backup
                        shutil.copy2(target, backup)
                        if (
                            hashlib.sha256(backup.read_bytes()).hexdigest()
                            != original_digest
                        ):
                            raise ValueError(
                                f"parent artifact changed while backing up: {target}"
                            )
                    try:
                        for temporary, target, _original_digest, _digest in prepared:
                            temporary.replace(target)
                            replaced.append(target)
                    except BaseException as exc:
                        failures = self._rollback_artifacts(prepared, backups, replaced)
                        if failures:
                            retained_backups.update(
                                item for item in failures if item in backups.values()
                            )
                            locations = ", ".join(str(item) for item in failures)
                            raise OSError(
                                "artifact publish failed and rollback needs recovery: "
                                f"{locations}"
                            ) from exc
                        raise
                except BaseException:
                    self._remove_created_directories(created_directories)
                    raise
        finally:
            shutil.rmtree(stage_root, ignore_errors=True)
            for temporary in publish_temporaries:
                temporary.unlink(missing_ok=True)
            for backup in backups.values():
                if backup not in retained_backups:
                    backup.unlink(missing_ok=True)

    def _create_parent_directories(self, parent: Path) -> list[Path]:
        missing: list[Path] = []
        current = parent
        while current != self.parent_workspace and not current.exists():
            missing.append(current)
            current = current.parent
        parent.mkdir(parents=True, exist_ok=True)
        return list(reversed(missing))

    @staticmethod
    def _revalidate_parent_artifacts(
        staged: list[tuple[Path, Path, str | None, str]],
    ) -> None:
        for _temporary, target, original_digest, _digest in staged:
            relative = target.name
            if original_digest is None:
                if target.exists():
                    raise ValueError(
                        f"parent artifact was created concurrently: {relative}"
                    )
            elif (
                not target.is_file()
                or hashlib.sha256(target.read_bytes()).hexdigest() != original_digest
            ):
                raise ValueError(
                    f"parent artifact changed after child snapshot: {relative}"
                )

    @staticmethod
    def _remove_created_directories(directories: list[Path]) -> None:
        for directory in reversed(directories):
            try:
                directory.rmdir()
            except OSError:
                pass

    @staticmethod
    def _rollback_artifacts(
        staged: list[tuple[Path, Path, str | None, str]],
        backups: dict[Path, Path],
        replaced: list[Path],
    ) -> list[Path]:
        failures: list[Path] = []
        digests = {target: digest for _temp, target, _original, digest in staged}
        for target in reversed(replaced):
            try:
                if (
                    not target.is_file()
                    or hashlib.sha256(target.read_bytes()).hexdigest()
                    != digests[target]
                ):
                    failures.append(backups.get(target, target))
                    continue
                backup = backups.get(target)
                if backup is None:
                    target.unlink()
                else:
                    backup.replace(target)
            except OSError:
                failures.append(backups.get(target, target))
        return failures

    def cleanup(self, snapshot: WorkspaceSnapshot) -> None:
        root = snapshot.root.resolve()
        if root == self.state_root or self.state_root not in root.parents:
            raise ValueError("refusing to remove a path outside agent state")
        shutil.rmtree(root)
        self._baselines.pop(root, None)

    def _copy_tree(
        self,
        source: Path,
        target: Path,
        totals: list[int],
        manifest: dict[str, str],
    ) -> None:
        for entry in source.iterdir():
            if entry.name in _EXCLUDED_NAMES or _is_private_name(entry.name):
                continue
            if entry.is_symlink():
                raise ValueError(f"symlink is not allowed in child snapshot: {entry}")
            destination = target / entry.name
            if entry.is_dir():
                destination.mkdir()
                self._copy_tree(entry, destination, totals, manifest)
            elif entry.is_file():
                totals[0] += 1
                totals[1] += entry.stat().st_size
                if totals[0] > self.max_files or totals[1] > self.max_bytes:
                    raise ValueError(
                        "child workspace snapshot exceeds configured limits"
                    )
                shutil.copy2(entry, destination)
                relative = str(entry.relative_to(self.parent_workspace))
                manifest[relative] = hashlib.sha256(entry.read_bytes()).hexdigest()
