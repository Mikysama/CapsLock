"""Asynchronous context construction and citation resolution."""

from __future__ import annotations

import re
from typing import Any

from ..evidence import Evidence
from ..ports import WorkspaceServicesPort


class ContextBuilder:
    def __init__(
        self,
        repositories: WorkspaceServicesPort,
        max_messages: int,
        instructions: str,
        memory: Any = None,
    ) -> None:
        self.repositories = repositories
        self.max_messages = max_messages
        self.instructions = instructions
        self.memory = memory
        self.last_recalls: list[Any] = []

    async def build(
        self, session_id: str, question: str, *, run_id: str
    ) -> list[dict[str, object]]:
        excluded = (
            await self.memory.excluded_runs() if self.memory is not None else set()
        )
        summary = ""
        if (
            await self.repositories.sessions.message_count(
                session_id, excluded_run_ids=excluded
            )
            > self.max_messages
        ):
            summary = await self.repositories.sessions.compact_summary(
                session_id, self.max_messages, excluded_run_ids=excluded
            )
        history = await self.repositories.sessions.context_messages(
            session_id,
            self.max_messages,
            excluded_run_ids=excluded | {run_id},
        )
        memory_context = ""
        self.last_recalls = []
        if self.memory is not None:
            memory_context, self.last_recalls = await self.memory.recall_context(
                question, run_id=run_id
            )
        system = self.instructions + (
            f"\nEarlier session summary:\n{summary}" if summary else ""
        )
        if memory_context:
            system += "\n\n" + memory_context
        return [
            {"role": "system", "content": system},
            *history,
            {"role": "user", "content": question},
        ]


class CitationResolver:
    def __init__(self, repositories: WorkspaceServicesPort) -> None:
        self.repositories = repositories

    async def resolve(
        self,
        text: str,
        *,
        evidence: dict[str, Evidence],
        source_ids: set[str],
        memories: dict[str, object],
        session_id: str,
    ) -> tuple[str, list[object]]:
        evidence_ids = re.findall(r"\[\[evidence:(ev_[a-f0-9]+)\]\]", text)
        citations: list[object] = [
            evidence[item] for item in evidence_ids if item in evidence
        ]
        for source_id in re.findall(r"\[\[source:([a-f0-9]+)\]\]", text):
            source = (
                await self.repositories.sources.get(source_id, session_id=session_id)
                if source_id in source_ids
                else None
            )
            if source is not None:
                citations.append(source)
        for memory_id in re.findall(r"\[\[memory:(mem_[a-f0-9]+)\]\]", text):
            if memory_id in memories:
                citations.append(memories[memory_id])
        if evidence and not citations:
            citations = list(evidence.values())
        cleaned = re.sub(
            r"\s*\[\[(?:evidence:ev_[a-f0-9]+|source:[a-f0-9]+|memory:mem_[a-f0-9]+)\]\]",
            "",
            text,
        ).strip()
        return cleaned, _unique(citations)


def citation_data(item: Any) -> dict[str, object]:
    if hasattr(item, "path"):
        return {
            "kind": "evidence",
            "id": item.id,
            "path": str(item.path),
            "start_line": item.start_line,
            "end_line": item.end_line,
        }
    if hasattr(item, "url"):
        return {
            "kind": "source",
            "id": item.id,
            "url": item.url,
            "title": item.title,
            "fetched_at": item.fetched_at,
        }
    return {
        "kind": "memory",
        "id": item.id,
        "type": item.type.value,
        "scope": item.scope.value,
        "source_kind": item.source_kind,
    }


def _unique(items: list[Any]) -> list[Any]:
    seen, output = set(), []
    for item in items:
        if item.id not in seen:
            seen.add(item.id)
            output.append(item)
    return output
