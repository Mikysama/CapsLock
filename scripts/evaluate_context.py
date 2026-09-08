#!/usr/bin/env python3
"""Evaluate position-sensitive recovery from transcript, compaction, and tools."""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import tempfile
from pathlib import Path

from capslock.storage.artifacts import ToolArtifactStore
from capslock.storage.repositories import WorkspaceRepositories
from capslock.application.workflow import WorkflowService


ROOT = Path(__file__).resolve().parents[1]
FIXTURE = ROOT / "tests" / "fixtures" / "context_position_quality.json"


def _positioned(canary: str, position: str) -> list[str]:
    distractors = [f"irrelevant record {index}: " + "x" * 300 for index in range(24)]
    index = {"front": 0, "middle": len(distractors) // 2, "tail": len(distractors)}[
        position
    ]
    distractors.insert(index, f"The exact CapsLock canary is {canary}")
    return distractors


async def deterministic() -> dict[str, object]:
    fixture = json.loads(FIXTURE.read_text(encoding="utf-8"))
    canary = str(fixture["canary"])
    results = []
    with tempfile.TemporaryDirectory(prefix="capslock-context-eval-") as temporary:
        root = Path(temporary)
        repositories = await WorkspaceRepositories.open(
            root / "state.db", workspace=root
        )
        try:
            for position in fixture["positions"]:
                for kind in fixture["kinds"]:
                    session = await repositories.sessions.create("evaluation")
                    workflow = WorkflowService(
                        repositories.work_items,
                        repositories.runs,
                        repositories.run_journal,
                        repositories.workflow,
                    )
                    prepared = await workflow.prepare(session.id, f"{kind}-{position}")
                    records = _positioned(canary, position)
                    if kind in {"transcript", "compaction"}:
                        for record in records:
                            await repositories.sessions.append_message(
                                session.id, prepared.run.id, "user", record
                            )
                    elif kind == "tool_result":
                        await repositories.episodic.index(
                            session_id=session.id,
                            run_id=prepared.run.id,
                            source_kind="tool_result",
                            source_id=f"tool-{position}",
                            content="\n".join(records),
                        )
                    else:
                        store = ToolArtifactStore(
                            root / "artifacts",
                            repositories.database,
                            repositories.episodic,
                        )
                        await store.put(
                            session_id=session.id,
                            run_id=prepared.run.id,
                            content="\n".join(records).encode(),
                        )
                    hits = await repositories.episodic.search(
                        "CapsLock canary", session_id=session.id, limit=5
                    )
                    passed = any(canary in hit.content for hit in hits)
                    results.append(
                        {"position": position, "kind": kind, "passed": passed}
                    )
        finally:
            await repositories.close()
    by_position = {
        position: sum(
            item["passed"] for item in results if item["position"] == position
        )
        / sum(1 for item in results if item["position"] == position)
        for position in fixture["positions"]
    }
    return {
        "mode": "deterministic",
        "cases": len(results),
        "passed": sum(item["passed"] for item in results),
        "by_position": by_position,
        "position_gap": max(by_position.values()) - min(by_position.values()),
        "results": results,
    }


async def live(model: str) -> dict[str, object]:
    from openai import AsyncOpenAI

    fixture = json.loads(FIXTURE.read_text(encoding="utf-8"))
    client = AsyncOpenAI(
        api_key=os.environ.get("OPENAI_API_KEY") or os.environ.get("CAPSLOCK_API_KEY"),
        base_url=os.environ.get("OPENAI_BASE_URL")
        or os.environ.get("CAPSLOCK_BASE_URL"),
    )
    samples = []
    try:
        for position in fixture["positions"]:
            prompt = "\n".join(
                [
                    *_positioned(str(fixture["canary"]), position),
                    str(fixture["question"]),
                ]
            )
            response = await client.responses.create(model=model, input=prompt)
            text = response.output_text
            samples.append(
                {
                    "position": position,
                    "passed": str(fixture["canary"]) in text,
                }
            )
    finally:
        await client.close()
    by_position = {
        position: float(
            next(item["passed"] for item in samples if item["position"] == position)
        )
        for position in fixture["positions"]
    }
    return {
        "mode": "live",
        "model": model,
        "by_position": by_position,
        "position_gap": max(by_position.values()) - min(by_position.values()),
        "samples": samples,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--mode", choices=("deterministic", "live"), default="deterministic"
    )
    parser.add_argument("--model", default="gpt-4.1-mini")
    args = parser.parse_args()
    result = asyncio.run(live(args.model) if args.mode == "live" else deterministic())
    print(json.dumps(result, ensure_ascii=False, indent=2))
    minimum = 1.0 if args.mode == "deterministic" else 0.95
    return (
        0
        if min(result["by_position"].values()) >= minimum
        and result["position_gap"] <= 0.05
        else 1
    )


if __name__ == "__main__":
    raise SystemExit(main())
