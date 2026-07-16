"""Per-run Skill snapshots used by explicit and model-driven loading."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable

from .manifest import SkillPackage, SkillValidationError
from .registry import SkillRegistry


MAX_RESOURCE_READ_BYTES = 512 * 1024


@dataclass(frozen=True)
class LoadedSkill:
    package: SkillPackage
    resources: dict[str, bytes]


class SkillService:
    def __init__(self, registry: SkillRegistry, event: Callable[..., None]) -> None:
        self.registry = registry
        self.event = event
        self._loaded: dict[tuple[str, str], LoadedSkill] = {}

    def load(self, run_id: str, name: str, *, trigger: str) -> LoadedSkill:
        key = (run_id, name)
        loaded = self._loaded.get(key)
        if loaded is not None:
            return loaded
        package = self.registry.get(name)
        try:
            resources = {
                item.path: package.root.joinpath(item.path).read_bytes()
                for item in package.resources
            }
        except OSError as exc:
            raise SkillValidationError(
                f"Skill resources cannot be snapshotted: {package.name}"
            ) from exc
        loaded = LoadedSkill(package, resources)
        self._loaded[key] = loaded
        self.event(
            "skill_loaded",
            run_id=run_id,
            name=package.name,
            scope=package.scope,
            digest=package.digest,
            trigger=trigger,
        )
        return loaded

    def load_data(self, run_id: str, name: str, *, trigger: str) -> tuple[dict[str, object], dict[str, object]]:
        loaded = self.load(run_id, name, trigger=trigger)
        package = loaded.package
        resources = [
            {"path": item.path, "size": item.size, "kind": item.kind}
            for item in package.resources
        ]
        data = {
            "name": package.name,
            "description": package.description,
            "scope": package.scope,
            "digest": package.digest,
            "instructions": package.instructions,
            "resources": resources,
        }
        audit = {
            "name": package.name,
            "scope": package.scope,
            "digest": package.digest,
            "resource_count": len(resources),
            "trigger": trigger,
        }
        return data, audit

    def read_resource(
        self,
        run_id: str,
        name: str,
        path: str,
        *,
        start_line: int = 1,
        end_line: int | None = None,
    ) -> tuple[dict[str, object], dict[str, object]]:
        loaded = self._loaded.get((run_id, name))
        if loaded is None:
            raise SkillValidationError(f"Skill must be loaded before reading resources: {name}")
        resource = loaded.package.resource(path)
        content = loaded.resources[resource.path]
        if len(content) > MAX_RESOURCE_READ_BYTES:
            raise SkillValidationError(
                f"Skill resource exceeds the {MAX_RESOURCE_READ_BYTES} byte read limit: {path}"
            )
        try:
            text = content.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise SkillValidationError(f"Skill resource is binary or non-UTF-8: {path}") from exc
        lines = text.splitlines()
        final_line = len(lines) if end_line is None else end_line
        if (
            start_line < 1
            or final_line < start_line
            or start_line > len(lines)
            or final_line > len(lines)
        ):
            raise SkillValidationError(
                f"line range must be within the resource's 1-{len(lines)} lines"
            )
        data = {
            "skill": name,
            "path": resource.path,
            "kind": resource.kind,
            "start_line": start_line,
            "end_line": final_line,
            "text": "\n".join(lines[start_line - 1 : final_line]),
        }
        audit = {
            "skill": name,
            "path": resource.path,
            "kind": resource.kind,
            "start_line": start_line,
            "end_line": final_line,
            "digest": loaded.package.digest,
        }
        return data, audit

    def finish_run(self, run_id: str) -> None:
        for key in [key for key in self._loaded if key[0] == run_id]:
            del self._loaded[key]
