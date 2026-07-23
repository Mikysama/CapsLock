"""Session-scoped, content-addressed tool result storage."""

from __future__ import annotations

import hashlib
import os
import tempfile
import uuid
from dataclasses import dataclass
from pathlib import Path

from ..security import redact
from .repositories.core import now


MAX_ARTIFACT_BYTES = 5 * 1024 * 1024
MAX_READ_BYTES = 16 * 1024


class ArtifactAccessError(ValueError):
    code = "artifact_access_denied"


@dataclass(frozen=True)
class ToolArtifact:
    id: str
    session_id: str
    run_id: str
    sha256: str
    size_bytes: int
    media_type: str
    preview: str


class ToolArtifactStore:
    def __init__(self, root: Path, database) -> None:
        self.root = root.resolve()
        self.database = database

    async def put(
        self,
        *,
        session_id: str,
        run_id: str,
        content: bytes,
        invocation_id: str | None = None,
        media_type: str = "application/json",
    ) -> ToolArtifact:
        if len(content) > MAX_ARTIFACT_BYTES:
            raise ValueError(
                f"tool result exceeds the {MAX_ARTIFACT_BYTES} byte hard limit"
            )
        digest = hashlib.sha256(content).hexdigest()
        relative = Path("sha256") / digest[:2] / digest
        target = self.root / relative
        await _atomic_write(target, content)
        preview = _preview(content)
        identifier = f"artifact_{uuid.uuid4().hex}"
        try:
            await self.database.execute(
                """INSERT INTO tool_artifacts(id,session_id,run_id,invocation_id,sha256,size_bytes,media_type,relative_path,preview,created_at)
                   VALUES(?,?,?,?,?,?,?,?,?,?)""",
                (
                    identifier,
                    session_id,
                    run_id,
                    invocation_id,
                    digest,
                    len(content),
                    media_type,
                    relative.as_posix(),
                    preview,
                    now(),
                ),
            )
        except Exception:
            row = await self.database.fetch_one(
                "SELECT * FROM tool_artifacts WHERE session_id=? AND sha256=?",
                (session_id, digest),
            )
            if row is None:
                raise
            return _record(row)
        return ToolArtifact(
            identifier, session_id, run_id, digest, len(content), media_type, preview
        )

    async def read(
        self,
        artifact_id: str,
        *,
        session_id: str,
        offset: int = 0,
        limit: int = MAX_READ_BYTES,
    ) -> tuple[ToolArtifact, bytes, bool]:
        if offset < 0 or not 1 <= limit <= MAX_READ_BYTES:
            raise ValueError("invalid artifact read range")
        row = await self.database.fetch_one(
            "SELECT * FROM tool_artifacts WHERE id=? AND session_id=?",
            (artifact_id, session_id),
        )
        if row is None:
            raise ArtifactAccessError("artifact is not visible to this session")
        record = _record(row)
        target = self._path(str(row["relative_path"]))
        content = target.read_bytes()
        if (
            len(content) != record.size_bytes
            or hashlib.sha256(content).hexdigest() != record.sha256
        ):
            raise ArtifactAccessError("artifact integrity check failed")
        chunk = content[offset : offset + limit]
        return record, chunk, offset + len(chunk) < len(content)

    async def cleanup_session(self, session_id: str) -> None:
        rows = await self.database.fetch_all(
            "SELECT relative_path FROM tool_artifacts WHERE session_id=?",
            (session_id,),
        )
        await self.database.execute(
            "DELETE FROM tool_artifacts WHERE session_id=?", (session_id,)
        )
        for row in rows:
            relative = str(row["relative_path"])
            remaining = await self.database.fetch_one(
                "SELECT 1 FROM tool_artifacts WHERE relative_path=? LIMIT 1",
                (relative,),
            )
            if remaining is None:
                self._path(relative).unlink(missing_ok=True)

    def _path(self, relative: str) -> Path:
        target = (self.root / relative).resolve()
        if not target.is_relative_to(self.root):
            raise ArtifactAccessError("invalid artifact path")
        return target


async def _atomic_write(path: Path, content: bytes) -> None:
    if path.exists():
        return
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    descriptor, temporary = tempfile.mkstemp(prefix=".artifact-", dir=path.parent)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        os.chmod(temporary, 0o600)
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def _preview(content: bytes) -> str:
    text = content[:4096].decode("utf-8", errors="replace")
    safe = redact({"preview": text})["preview"]
    return str(safe)


def _record(row) -> ToolArtifact:
    return ToolArtifact(
        str(row["id"]),
        str(row["session_id"]),
        str(row["run_id"]),
        str(row["sha256"]),
        int(row["size_bytes"]),
        str(row["media_type"]),
        str(row["preview"]),
    )
