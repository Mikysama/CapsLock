"""OS sandbox construction and session-scoped process jobs."""

from __future__ import annotations

import asyncio
import os
import shutil
import signal
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .sandbox import SandboxedCommand


@dataclass
class ProcessJob:
    id: str
    session_id: str
    process: asyncio.subprocess.Process
    output_limit: int
    temporary: Path
    stdout: bytearray = field(default_factory=bytearray)
    stderr: bytearray = field(default_factory=bytearray)
    tasks: tuple[asyncio.Task[Any], ...] = ()

    @property
    def status(self) -> str:
        return "running" if self.process.returncode is None else "completed"


class SessionProcessManager:
    def __init__(self, output_limit: int = 100_000) -> None:
        self.output_limit = output_limit
        self._jobs: dict[str, ProcessJob] = {}

    async def start(self, session_id: str, command: SandboxedCommand) -> ProcessJob:
        process = await asyncio.create_subprocess_exec(
            *command.argv,
            cwd=command.cwd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            start_new_session=True,
        )
        identifier = f"proc_{uuid.uuid4().hex}"
        job = ProcessJob(
            identifier, session_id, process, self.output_limit, command.temporary
        )
        assert process.stdout is not None and process.stderr is not None
        captures = (
            asyncio.create_task(self._capture(process.stdout, job.stdout)),
            asyncio.create_task(self._capture(process.stderr, job.stderr)),
        )
        cleanup = asyncio.create_task(self._cleanup(job, captures))
        job.tasks = (*captures, cleanup)
        self._jobs[identifier] = job
        return job

    async def _capture(self, stream: asyncio.StreamReader, target: bytearray) -> None:
        while data := await stream.read(8192):
            remaining = self.output_limit - len(target)
            if remaining > 0:
                target.extend(data[:remaining])

    async def _cleanup(
        self, job: ProcessJob, captures: tuple[asyncio.Task[Any], ...]
    ) -> None:
        await job.process.wait()
        await asyncio.gather(*captures, return_exceptions=True)
        shutil.rmtree(job.temporary, ignore_errors=True)

    def get(self, session_id: str, identifier: str) -> ProcessJob:
        job = self._jobs.get(identifier)
        if job is None or job.session_id != session_id:
            raise ValueError("background process does not exist in this session")
        return job

    def has_active(self, session_id: str) -> bool:
        return any(
            job.session_id == session_id and job.process.returncode is None
            for job in self._jobs.values()
        )

    async def stop(self, session_id: str, identifier: str) -> ProcessJob:
        job = self.get(session_id, identifier)
        await stop_process(job.process)
        await asyncio.gather(*job.tasks, return_exceptions=True)
        return job

    async def close(self) -> None:
        await asyncio.gather(
            *(
                self.stop(job.session_id, job.id)
                for job in tuple(self._jobs.values())
                if job.process.returncode is None
            ),
            return_exceptions=True,
        )
        await asyncio.gather(
            *(task for job in self._jobs.values() for task in job.tasks),
            return_exceptions=True,
        )


async def stop_process(process: asyncio.subprocess.Process) -> None:
    if process.returncode is not None:
        return
    try:
        os.killpg(process.pid, signal.SIGTERM)
    except ProcessLookupError:
        return
    try:
        await asyncio.wait_for(process.wait(), 2)
    except TimeoutError:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        await process.wait()
