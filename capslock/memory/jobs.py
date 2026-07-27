"""In-process executor backed by the durable memory job table."""

from __future__ import annotations

import asyncio

from ..domain import MemoryJobStatus, MemoryJobType


class MemoryJobWorker:
    def __init__(self, service) -> None:
        self.service = service
        self._tasks: set[asyncio.Task] = set()
        self._closing = False

    async def recover(self) -> int:
        return await self.service.repositories.jobs.recover()

    def wake(self, chat_model, *, model: str) -> None:
        if self._closing:
            return
        task = asyncio.create_task(
            self._drain(chat_model, model=model), name="capslock-memory-jobs"
        )
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def _drain(self, chat_model, *, model: str) -> None:
        while not self._closing:
            job = await self.service.repositories.jobs.claim(
                workspace=self.service.workspace_key,
                job_type=MemoryJobType.EXTRACT_RUN,
            )
            if job is None:
                return
            try:
                payload = job["payload"]
                view = await self.service.settings()
                result = await self.service.candidate_service.capture(
                    chat_model,
                    model=str(payload.get("model") or model),
                    run_id=str(job["run_id"]),
                    question="",
                    answer="",
                    envelope=payload["envelope"],
                    write_enabled=view.write_enabled,
                    policy_override=view.policy,
                    raise_errors=True,
                )
                if result.extraction_id is None:
                    raise RuntimeError("capture_disabled")
                await self.service.repositories.jobs.complete(str(job["id"]))
            except Exception as exc:
                state = await self.service.repositories.jobs.fail(
                    str(job["id"]), type(exc).__name__
                )
                self.service.event(
                    "memory_job_failed",
                    job_id=job["id"],
                    error=type(exc).__name__,
                    terminal=state is MemoryJobStatus.FAILED,
                )
                if state is MemoryJobStatus.QUEUED:
                    await asyncio.sleep(2 ** max(0, int(job["attempt_count"]) - 1))
                    continue

    async def close(self, timeout: float = 10.0) -> None:
        self._closing = True
        if not self._tasks:
            return
        done, pending = await asyncio.wait(self._tasks, timeout=timeout)
        for task in pending:
            task.cancel()
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)
