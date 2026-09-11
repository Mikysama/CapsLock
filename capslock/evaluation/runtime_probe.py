"""Candidate-aware probe backed by the real CapsLock workspace runtime."""

from __future__ import annotations

import os
import time
from dataclasses import replace
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any

from openai import AsyncOpenAI

from ..bootstrap import WorkspaceApplication
from ..configuration import Settings
from ..domain import AgentEventKind, RunKind, RunMode
from ..memory.recall import RecallPolicy
from ..runtime import RunRequest
from .models import EvaluationTask, PolicyCandidate
from .runner import _answer_matches, _expected_answer, _live_prompt
from .context_compaction import policy_for_candidate


class _ResponsesProxy:
    def __init__(self, responses: Any) -> None:
        self._responses = responses

    async def create(self, **kwargs: Any) -> Any:
        extra = kwargs.get("extra_body")
        if not isinstance(extra, dict):
            extra = {}
            kwargs["extra_body"] = extra
        thinking = extra.get("thinking")
        if not isinstance(thinking, dict):
            thinking = {}
            extra["thinking"] = thinking
        thinking.setdefault("type", "disabled")
        return await self._responses.create(**kwargs)


class _ThinkingDisabledClient:
    """OpenAI client facade used only by the evaluation Runtime."""

    def __init__(self, client: AsyncOpenAI) -> None:
        self._client = client
        self.responses = _ResponsesProxy(client.responses)


class WorkspaceRuntimeCandidateProbe:
    """Run one evaluation task through a fresh, isolated AgentSession.

    A fresh workspace prevents task state, memory, and persisted events from
    leaking across candidates or repetitions. The returned object matches the
    rich candidate-probe protocol accepted by :class:`EvaluationRunner`.
    """

    def __init__(self, *, workspace_root: Path | None = None) -> None:
        self.workspace_root = workspace_root

    async def __call__(
        self,
        task: EvaluationTask,
        candidate: PolicyCandidate,
        provider: str,
        model: str,
    ) -> dict[str, Any]:
        started = time.monotonic()
        with TemporaryDirectory(
            prefix="capslock-eval-runtime-",
            dir=str(self.workspace_root) if self.workspace_root else None,
        ) as directory:
            root = Path(directory)
            settings, memory_policy = _settings_for_candidate(
                Settings.load(root), candidate, provider=provider, model=model
            )
            key = os.environ.get(f"{provider.upper()}_API_KEY")
            if not key:
                raise RuntimeError(f"missing {provider.upper()}_API_KEY")
            base_url = os.environ.get(f"{provider.upper()}_BASE_URL")
            client = AsyncOpenAI(
                api_key=key,
                base_url=base_url or settings.model_config.base_url,
                timeout=settings.model_config.timeout_seconds,
            )
            runtime_client = _ThinkingDisabledClient(client)
            application: WorkspaceApplication | None = None
            events = []
            try:
                application = await WorkspaceApplication.open(
                    workspace=root,
                    settings=settings,
                    client={"default": runtime_client},
                    close_client=False,
                    memory_recall_policy=memory_policy,
                    context_evaluation_policy=policy_for_candidate(candidate),
                )
                history = task.requirements.get("history")
                if isinstance(history, list):
                    seed_run = await application.repositories.runs.create_hidden(
                        application.session.session_id,
                        kind=RunKind.SESSION_SEED,
                    )
                    for message in history:
                        if not isinstance(message, dict):
                            continue
                        await application.repositories.sessions.append_message(
                            application.session.session_id,
                            seed_run.id,
                            (
                                "assistant"
                                if message.get("role") == "tool"
                                else str(message.get("role", "user"))
                            ),
                            (
                                "[Tool result "
                                + str(message.get("tool_call_id", "unknown"))
                                + "]\n"
                                + str(message.get("content", ""))
                                if message.get("role") == "tool"
                                else str(message.get("content", ""))
                            ),
                        )
                    await application.repositories.runs.finish_hidden(seed_run.id)
                async for event in application.session.run_stream(
                    RunRequest(question=_live_prompt(task), mode=RunMode.EXEC)
                ):
                    events.append(event)
            finally:
                if application is not None:
                    await application.close()
                else:
                    await client.close()

        terminal = next((event for event in reversed(events) if event.terminal), None)
        payload = terminal.data if terminal is not None else {}
        answer = str(payload.get("answer", "")).strip()
        usage = payload.get("usage")
        usage = usage if isinstance(usage, dict) else {}
        expected = _expected_answer(task)
        stop_reason = payload.get("stop_reason")
        if (
            stop_reason is None
            and terminal is not None
            and terminal.kind is not AgentEventKind.COMPLETED
        ):
            stop_reason = str(payload.get("error", {}).get("code", terminal.kind.value))
        metrics: dict[str, Any] = {
            "tool_calls": sum(
                event.kind is AgentEventKind.TOOL_QUEUED for event in events
            ),
            "compaction_events": sum(
                event.kind is AgentEventKind.CONTEXT_UPDATED for event in events
            ),
            "approval_events": sum(
                event.kind
                in {AgentEventKind.TOOL_PERMISSION, AgentEventKind.WAITING_APPROVAL}
                for event in events
            ),
            "conflicts": sum("conflict" in str(event.data).lower() for event in events),
            "runtime_terminal_kind": terminal.kind.value if terminal else "missing",
        }
        return {
            "success": terminal is not None
            and terminal.kind is AgentEventKind.COMPLETED
            and stop_reason is None
            and _answer_matches(answer, expected),
            "input_tokens": int(usage.get("input_tokens", 0)),
            "output_tokens": int(usage.get("output_tokens", 0)),
            "stop_reason": stop_reason,
            "metrics": metrics,
            "runtime_latency_seconds": round(time.monotonic() - started, 6),
        }


def _settings_for_candidate(
    settings: Settings,
    candidate: PolicyCandidate,
    *,
    provider: str,
    model: str,
) -> tuple[Settings, RecallPolicy]:
    values = candidate.values
    providers = dict(settings.providers or {})
    if providers:
        provider_name = next(iter(providers))
        provider_settings = providers[provider_name]
        providers[provider_name] = replace(
            provider_settings,
            timeout_seconds=float(values["providers.timeout_seconds"]),
        )
    profiles = {
        name: replace(profile, model=model)
        for name, profile in (settings.models or {}).items()
        if profile.provider == provider or profile.model == settings.model_config.model
    }
    if not profiles:
        profiles = dict(settings.models or {})
    profiles = {
        name: replace(profile, model=model) for name, profile in profiles.items()
    }
    runtime = replace(
        settings.runtime,
        max_tool_rounds=int(values["runtime.max_tool_rounds"]),
    )
    tools = replace(
        settings.tools,
        max_read_concurrency=int(values["tools.max_read_concurrency"]),
        max_argument_repair_attempts=int(values["tools.max_argument_repair_attempts"]),
    )
    context = replace(
        settings.context,
        trigger_ratio=float(values["context.trigger_ratio"]),
        target_ratio=float(values["context.target_ratio"]),
        preserve_recent_turns=int(values["context.preserve_recent_turns"]),
        preserve_recent_tokens=int(values["context.preserve_recent_tokens"]),
        max_compaction_failures=int(values["context.max_compaction_failures"]),
        episodic_recall_limit=int(values["memory.recall_limit"]),
        episodic_recall_bytes=int(values["memory.recall_bytes"]),
    )
    agents = replace(
        settings.agents,
        max_children=int(values["agents.max_children"]),
        max_concurrency=int(values["agents.max_concurrency"]),
        max_child_tool_rounds=int(values["agents.max_child_tool_rounds"]),
    )
    loop_detection = replace(
        settings.loop_detection,
        consecutive_repeats=int(values["loop_detection.consecutive_repeats"]),
        failed_retries=int(values["loop_detection.failed_retries"]),
        cycle_repetitions=int(values["loop_detection.cycle_repetitions"]),
        max_cycle_length=int(values["loop_detection.max_cycle_length"]),
    )
    primary = settings.model_config
    updated = replace(
        settings,
        model_config=replace(
            primary,
            model=model,
            timeout_seconds=float(values["providers.timeout_seconds"]),
        ),
        runtime=runtime,
        tools=tools,
        context=context,
        agents=agents,
        loop_detection=loop_detection,
        providers=providers,
        models=profiles,
    )
    memory_policy = RecallPolicy(
        limit=int(values["memory.recall_limit"]),
        byte_budget=int(values["memory.recall_bytes"]),
        semantic_threshold=float(values["memory.semantic_threshold"]),
        recall_threshold=float(values["memory.recall_threshold"]),
        retrieval_weight=float(values["memory.retrieval_weight"]),
        scope_weight=float(values["memory.scope_weight"]),
        confidence_weight=float(values["memory.confidence_weight"]),
        freshness_weight=float(values["memory.freshness_weight"]),
        source_validity_weight=float(values["memory.source_validity_weight"]),
    )
    return updated, memory_policy


async def probe(
    task: EvaluationTask,
    candidate: PolicyCandidate,
    provider: str,
    model: str,
) -> dict[str, Any]:
    """CLI-loadable adapter function for ``--candidate-probe``."""
    return await WorkspaceRuntimeCandidateProbe()(task, candidate, provider, model)


__all__ = ["WorkspaceRuntimeCandidateProbe", "probe"]
