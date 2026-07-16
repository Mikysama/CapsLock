#!/usr/bin/env python3
"""Run the deterministic v1.6 memory recall quality fixture."""

from __future__ import annotations

import argparse
import json
import tempfile
from datetime import UTC, datetime, timedelta
from pathlib import Path

from capslock.domain import MemoryPolicy, MemoryScope, MemoryType
from capslock.memory import MemoryService
from capslock.storage import MemoryStore


DEFAULT_FIXTURE = Path(__file__).resolve().parents[1] / "tests" / "fixtures" / "memory_quality.jsonl"


def evaluate(path: Path) -> dict[str, object]:
    cases = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
    passed = 0
    failures: list[str] = []
    with tempfile.TemporaryDirectory(prefix="capslock-memory-eval-") as directory:
        root = Path(directory)
        with MemoryStore(root / "memory.sqlite3") as store:
            for index, case in enumerate(cases):
                workspace = root / f"workspace-{index}"
                other_workspace = root / f"other-{index}"
                workspace.mkdir()
                other_workspace.mkdir()
                memory = MemoryService(store, workspace=workspace, session_id=f"session-{index}")
                memory.set_policy(MemoryPolicy.OFF)
                for content in case.get("visible", []):
                    memory.add(content=content, memory_type=MemoryType.FACT, scope=MemoryScope.WORKSPACE)
                for content in case.get("expired", []):
                    memory.add(
                        content=content,
                        memory_type=MemoryType.FACT,
                        scope=MemoryScope.WORKSPACE,
                        expires_at=(datetime.now(UTC) - timedelta(days=1)).isoformat(),
                    )
                for content in case.get("forgotten", []):
                    item, _ = memory.add(
                        content=content, memory_type=MemoryType.FACT, scope=MemoryScope.WORKSPACE
                    )
                    memory.forget(item.id)
                other = MemoryService(store, workspace=other_workspace, session_id=f"other-{index}")
                for content in case.get("other_workspace", []):
                    other.add(content=content, memory_type=MemoryType.FACT, scope=MemoryScope.WORKSPACE)
                contents = {
                    hit.memory.content for hit in memory.recall(case["query"], run_id=f"run-{index}")
                }
                ok = case["expected"] in contents and (
                    case.get("forbidden") is None or case["forbidden"] not in contents
                )
                if ok:
                    passed += 1
                else:
                    failures.append(case["name"])
    rate = passed / len(cases) if cases else 0.0
    return {"cases": len(cases), "passed": passed, "top5_hit_rate": rate, "failures": failures}


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
