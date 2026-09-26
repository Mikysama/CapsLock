"""Trusted selection state and content-free prefix/cache diagnostics."""

import hashlib
import json
from dataclasses import dataclass, field


@dataclass
class SelectionState:
    user_goal: str
    recent: tuple[tuple[str, bool], ...] = field(default_factory=tuple)

    def observe(self, outcomes, *, allowed_names: set[str]):
        self.recent = tuple(
            (name, bool(ok)) for name, ok in outcomes if name in allowed_names
        )[-12:]

    def query(self, *, planning: bool) -> str:
        # Only caller-supplied goal and allowlisted runtime outcomes are used.
        # Tool text, retrieved attachments and model messages never enter this state.
        state = " ".join(
            f"{name} {'succeeded' if ok else 'failed'}" for name, ok in self.recent
        )
        return f"{self.user_goal[:8192]}\nmode={'planning' if planning else 'execution'} {state}"


def prompt_metadata(messages: list[dict], schemas: list[dict]) -> dict:
    core = messages[0] if messages and messages[0].get("role") == "system" else {}
    core_bytes = json.dumps(
        core, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode()
    schema_bytes = json.dumps(
        schemas, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode()
    return {
        "core_prefix_sha256": hashlib.sha256(core_bytes).hexdigest(),
        "schema_sha256": hashlib.sha256(schema_bytes).hexdigest(),
        "core_prefix_bytes": len(core_bytes),
        "schema_bytes": len(schema_bytes),
        "estimated_schema_tokens": max(0, len(schema_bytes) // 4),
    }
