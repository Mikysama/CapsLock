"""Bounded stream handling and child cleanup shared by ripgrep tools."""

from __future__ import annotations

import asyncio
from pathlib import Path

from ....security import redact
from ...contracts import ToolOutcome


class RipgrepProcess:
    def __init__(self, process: asyncio.subprocess.Process) -> None:
        self.process = process
        self.stderr = bytearray()
        self.stderr_task = asyncio.create_task(self._drain_stderr())

    @classmethod
    async def start(
        cls, command: list[str], *, cwd: Path, stream_limit: int = 65_536
    ) -> RipgrepProcess:
        process = await asyncio.create_subprocess_exec(
            command[0],
            "--no-config",
            *command[1:],
            cwd=cwd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            limit=stream_limit,
        )
        return cls(process)

    async def __aenter__(self) -> RipgrepProcess:
        return self

    async def __aexit__(self, *exc: object) -> None:
        cleanup = asyncio.create_task(self._close())
        try:
            await asyncio.shield(cleanup)
        except asyncio.CancelledError:
            await cleanup
            raise

    async def record(self, separator: bytes = b"\n") -> bytes:
        assert self.process.stdout is not None
        try:
            return await self.process.stdout.readuntil(separator)
        except asyncio.IncompleteReadError as exc:
            return exc.partial

    async def _drain_stderr(self) -> None:
        assert self.process.stderr is not None
        while chunk := await self.process.stderr.read(4096):
            self.stderr.extend(chunk[: max(0, 4096 - len(self.stderr))])

    async def _drain_stdout(self) -> None:
        assert self.process.stdout is not None
        while await self.process.stdout.read(65_536):
            pass

    async def _close(self) -> None:
        # Unread stdout must be drained: a paused pipe can otherwise prevent
        # Process.wait() completing even after the child has been killed.
        stdout_task = asyncio.create_task(self._drain_stdout())
        if self.process.returncode is None:
            try:
                self.process.terminate()
            except ProcessLookupError:
                pass
        try:
            async with asyncio.timeout(1):
                await self.process.wait()
        except TimeoutError:
            try:
                self.process.kill()
            except ProcessLookupError:
                pass
            await self.process.wait()
        finally:
            await asyncio.gather(stdout_task, self.stderr_task)

    def failure(self) -> ToolOutcome:
        detail = redact(self.stderr.decode("utf-8", "replace"))
        return ToolOutcome.failure(
            f"ripgrep search failed: {detail}",
            code="search_failed",
            data={"backend": "ripgrep", "exit_code": self.process.returncode},
        )
