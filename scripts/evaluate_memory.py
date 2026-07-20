#!/usr/bin/env python3
"""Run the deterministic v2 memory recall quality fixture."""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import tempfile
from datetime import UTC, datetime, timedelta
from pathlib import Path

from capslock.domain import MemoryPolicy, MemoryScope, MemoryType
from capslock.memory import MemoryService
from capslock.storage import MemoryRepositories


DEFAULT_FIXTURE = (
    Path(__file__).resolve().parents[1] / "tests" / "fixtures" / "memory_quality.jsonl"
)


async def async_evaluate(path: Path) -> dict[str, object]:
    cases = [
        json.loads(line)
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    passed = 0
    failures: list[str] = []
    with tempfile.TemporaryDirectory(prefix="capslock-memory-eval-") as directory:
        root = Path(directory)
        previous_home = os.environ.get("CAPSLOCK_HOME")
        os.environ["CAPSLOCK_HOME"] = str(root / "user")
        repositories = await MemoryRepositories.open(root / "memory.sqlite3")
        try:
            for index, case in enumerate(cases):
                workspace = root / f"workspace-{index}"
                other_workspace = root / f"other-{index}"
                workspace.mkdir()
                other_workspace.mkdir()
                memory = MemoryService(
                    repositories, workspace=workspace, session_id=f"session-{index}"
                )
                await memory.set_policy(MemoryPolicy.OFF)
                for content in case.get("visible", []):
                    await memory.add(
                        content=content,
                        memory_type=MemoryType.FACT,
                        scope=MemoryScope.WORKSPACE,
                    )
                for content in case.get("expired", []):
                    await memory.add(
                        content=content,
                        memory_type=MemoryType.FACT,
                        scope=MemoryScope.WORKSPACE,
                        expires_at=(datetime.now(UTC) - timedelta(days=1)).isoformat(),
                    )
                for content in case.get("forgotten", []):
                    item, _ = await memory.add(
                        content=content,
                        memory_type=MemoryType.FACT,
                        scope=MemoryScope.WORKSPACE,
                    )
                    await memory.forget(item.id)
                other = MemoryService(
                    repositories,
                    workspace=other_workspace,
                    session_id=f"other-{index}",
                )
                for content in case.get("other_workspace", []):
                    await other.add(
                        content=content,
                        memory_type=MemoryType.FACT,
                        scope=MemoryScope.WORKSPACE,
                    )
                _, hits = await memory.recall_context(
                    case["query"], run_id=f"run-{index}"
                )
                contents = {hit.memory.content for hit in hits}
                ok = case["expected"] in contents and (
                    case.get("forbidden") is None or case["forbidden"] not in contents
                )
                if ok:
                    passed += 1
                else:
                    failures.append(case["name"])
        finally:
            await repositories.close()
            if previous_home is None:
                os.environ.pop("CAPSLOCK_HOME", None)
            else:
                os.environ["CAPSLOCK_HOME"] = previous_home
    rate = passed / len(cases) if cases else 0.0
    return {
        "cases": len(cases),
        "passed": passed,
        "top5_hit_rate": rate,
        "failures": failures,
    }


def evaluate(path: Path) -> dict[str, object]:
    return asyncio.run(async_evaluate(path))


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--fixture", type=Path, default=DEFAULT_FIXTURE)
    parser.add_argument("--minimum", type=float, default=0.90)
    args = parser.parse_args()
    result = evaluate(args.fixture)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if float(result["top5_hit_rate"]) >= args.minimum else 1


if __name__ == "__main__":
    raise SystemExit(main())
