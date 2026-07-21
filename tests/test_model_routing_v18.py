from __future__ import annotations

import asyncio
import sqlite3
from pathlib import Path
from types import SimpleNamespace

import pytest

from capslock.config import (
    BudgetSettings,
    ModelProfileSettings,
    ProviderSettings,
    RoutingSettings,
    Settings,
)
from capslock.domain import (
    MemoryScope,
    MemoryType,
    ModelBudgetExceeded,
    ModelDataPolicyMismatch,
    ModelRoutingError,
)
from capslock.memory import MemoryService
from capslock.memory.embeddings import ExternalEmbeddingConfig
from capslock.runtime.model import ModelDelta, ModelMessage, ModelResponse, ModelUsage
from capslock.runtime.model import ModelRunContext
from capslock.runtime.routing import ModelRouter, _retry_delay
from capslock.storage.memory_v2 import MemoryRepositories
from capslock.storage.async_database import IncompatibleDatabaseError, WorkspaceDatabase
from capslock.storage.repositories_v2 import WorkspaceRepositories

from .helpers import workspace_run


class TransportError(RuntimeError):
    status_code = 503


class ScriptedClient:
    def __init__(self, *responses) -> None:
        self.responses = list(responses)
        self.calls = 0

    async def complete(self, **request):
        self.calls += 1
        response = self.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return response


class StreamingClient:
    def __init__(self, *scripts) -> None:
        self.scripts = list(scripts)
        self.calls = 0

    async def stream_complete(self, **request):
        self.calls += 1
        for item in self.scripts.pop(0):
            if isinstance(item, Exception):
                raise item
            yield item


def provider(name: str, *, policy: str = "shared") -> ProviderSettings:
    return ProviderSettings(
        name,
        "openai_compatible",
        f"https://{name}.example.test",
        f"{name.upper()}_KEY",
        "secret",
        10,
        policy,
    )


def profile(name: str, provider_name: str) -> ModelProfileSettings:
    return ModelProfileSettings(name, provider_name, name, 10_000, 100, 1, 2)


def test_multi_provider_config_and_legacy_mix_rejection(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("CAPSLOCK_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("PRIMARY_KEY", "secret")
    config = tmp_path / ".capslock"
    config.mkdir()
    document = """
[providers.primary]
base_url = "https://models.example.test/v1"
api_key_env = "PRIMARY_KEY"
data_policy = "company-approved"
[models.main]
provider = "primary"
model = "reasoner"
context_window = 64000
max_output_tokens = 2048
input_cost_per_million = 1
output_cost_per_million = 2
[models.quick]
provider = "primary"
model = "classifier"
[routing]
reasoning = ["main"]
fast = ["quick"]
embedding = ["quick"]
[budget]
max_run_tokens = 9000
max_run_usd = 1.5
"""
    (config / "config.toml").write_text(document, encoding="utf-8")
    settings = Settings.load(tmp_path)
    assert settings.routing.reasoning == ("main",)
    assert settings.routing.fast == ("quick",)
    assert settings.providers["primary"].api_key == "secret"
    assert settings.budget.max_run_tokens == 9000
    (config / "config.toml").write_text(
        document + "\n[model]\nmodel='legacy'\n", encoding="utf-8"
    )
    with pytest.raises(ValueError, match="legacy"):
        Settings.load(tmp_path)


def test_router_retries_and_falls_back_with_same_data_policy(tmp_path: Path) -> None:
    async def scenario() -> None:
        repositories = await WorkspaceRepositories.open(
            tmp_path / "state.sqlite3", workspace=tmp_path
        )
        try:
            _, prepared = await workspace_run(repositories)
            first = ScriptedClient(
                TransportError("down"),
                TransportError("down"),
                TransportError("down"),
            )
            second = ScriptedClient(
                ModelResponse(ModelMessage("ok"), ModelUsage(10, 5))
            )
            router = ModelRouter(
                providers={"one": provider("one"), "two": provider("two")},
                profiles={"a": profile("a", "one"), "b": profile("b", "two")},
                routing=RoutingSettings(("a", "b"), ("a",), (), ()),
                clients={"one": first, "two": second},
                audit=repositories.models,
            )
            response = await router.open_session(
                ModelRunContext(prepared.run.id)
            ).complete(model="ignored", messages=[], tools=[])
            assert response.message.content == "ok"
            assert first.calls == 3 and second.calls == 1
            calls = await repositories.database.fetch_all(
                "SELECT profile,status,fallback_from FROM model_calls ORDER BY started_at,id"
            )
            assert len(calls) == 4
            assert calls[-1]["profile"] == "b" and calls[-1]["fallback_from"] == "a"
            assert (await repositories.models.summary(prepared.run.id))[0][
                "errors"
            ] >= 0
        finally:
            await repositories.close()

    asyncio.run(scenario())


def test_router_explicit_run_session_records_usage_without_ambient_binding(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        repositories = await WorkspaceRepositories.open(
            tmp_path / "session.sqlite3", workspace=tmp_path
        )
        try:
            _, prepared = await workspace_run(repositories)
            provider_config = provider("provider", policy="local")
            profile_config = profile("reasoning", "provider")
            router = ModelRouter(
                providers={"provider": provider_config},
                profiles={"reasoning": profile_config},
                routing=RoutingSettings(("reasoning",), ("reasoning",), (), ()),
                clients={
                    "provider": ScriptedClient(
                        ModelResponse(ModelMessage("ok"), ModelUsage(2, 0))
                    )
                },
                audit=repositories.models,
            )
            model_session = router.open_session(ModelRunContext(prepared.run.id))
            response = await model_session.complete(
                model="ignored", messages=[], tools=[]
            )
            assert response.message.content == "ok"
            assert model_session.metered is True
            assert (await model_session.summary())[0]["role"] == "reasoning"
        finally:
            await repositories.close()

    asyncio.run(scenario())


def test_router_refuses_cross_policy_fallback_and_stops_before_budgeted_call(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        repositories = await WorkspaceRepositories.open(
            tmp_path / "state.sqlite3", workspace=tmp_path
        )
        try:
            _, prepared = await workspace_run(repositories)
            first = ScriptedClient(
                TransportError("down"),
                TransportError("down"),
                TransportError("down"),
            )
            second = ScriptedClient(ModelResponse(ModelMessage("must not run")))
            router = ModelRouter(
                providers={
                    "one": provider("one", policy="policy-a"),
                    "two": provider("two", policy="policy-b"),
                },
                profiles={"a": profile("a", "one"), "b": profile("b", "two")},
                routing=RoutingSettings(("a", "b"), ("a",), (), ()),
                clients={"one": first, "two": second},
                audit=repositories.models,
            )
            with pytest.raises(ModelDataPolicyMismatch):
                await router.open_session(ModelRunContext(prepared.run.id)).complete(
                    model="ignored", messages=[], tools=[]
                )
            assert second.calls == 0

            blocked = ScriptedClient(ModelResponse(ModelMessage("must not run")))
            budget_router = ModelRouter(
                providers={"one": provider("one")},
                profiles={"a": profile("a", "one")},
                routing=RoutingSettings(("a",), ("a",), (), ()),
                clients={"one": blocked},
                audit=repositories.models,
                budget=BudgetSettings(max_run_tokens=10),
            )
            with pytest.raises(ModelBudgetExceeded):
                await budget_router.open_session(
                    ModelRunContext(prepared.run.id)
                ).complete(model="ignored", messages=[], tools=[])
            assert blocked.calls == 0
            row = await repositories.database.fetch_one(
                "SELECT decision FROM budget_decisions ORDER BY id DESC LIMIT 1"
            )
            assert row[0] == "hard_stop"

            unmetered = ScriptedClient(ModelResponse(ModelMessage("no usage")))
            metered_router = ModelRouter(
                providers={"one": provider("one")},
                profiles={"a": profile("a", "one")},
                routing=RoutingSettings(("a",), ("a",), (), ()),
                clients={"one": unmetered},
                audit=repositories.models,
                budget=BudgetSettings(max_run_tokens=1000),
            )
            with pytest.raises(ModelRoutingError, match="did not return usage"):
                await metered_router.open_session(
                    ModelRunContext(prepared.run.id)
                ).complete(model="ignored", messages=[], tools=[])
            assert unmetered.calls == 1
        finally:
            await repositories.close()

    asyncio.run(scenario())


def test_retry_after_is_bounded() -> None:
    error = TransportError("limited")
    error.response = SimpleNamespace(headers={"retry-after": "99"})
    assert _retry_delay(error, 1) == 2.0


def test_stream_retry_only_happens_before_first_visible_delta(tmp_path: Path) -> None:
    async def scenario() -> None:
        repositories = await WorkspaceRepositories.open(
            tmp_path / "state.sqlite3", workspace=tmp_path
        )
        try:
            _, prepared = await workspace_run(repositories)
            retrying = StreamingClient(
                [TransportError("before output")],
                [ModelDelta(content="ok"), ModelDelta(usage=ModelUsage(2, 1))],
            )
            router = ModelRouter(
                providers={"one": provider("one")},
                profiles={"a": profile("a", "one")},
                routing=RoutingSettings(("a",), ("a",), (), ()),
                clients={"one": retrying},
                audit=repositories.models,
            )
            session = router.open_session(ModelRunContext(prepared.run.id))
            deltas = [
                item
                async for item in session.stream_complete(
                    model="ignored", messages=[], tools=[]
                )
            ]
            assert retrying.calls == 2
            assert "".join(item.content for item in deltas) == "ok"

            _, second_run = await workspace_run(repositories, "partial")
            partial = StreamingClient(
                [ModelDelta(content="visible"), TransportError("after output")],
                [ModelDelta(content="duplicate")],
            )
            router = ModelRouter(
                providers={"one": provider("one")},
                profiles={"a": profile("a", "one")},
                routing=RoutingSettings(("a",), ("a",), (), ()),
                clients={"one": partial},
                audit=repositories.models,
            )
            session = router.open_session(ModelRunContext(second_run.run.id))
            with pytest.raises(ModelRoutingError, match="retry suppressed"):
                async for _ in session.stream_complete(
                    model="ignored", messages=[], tools=[]
                ):
                    pass
            assert partial.calls == 1
        finally:
            await repositories.close()

    asyncio.run(scenario())


def test_schema_v1_is_backed_up_and_migrated_to_v2(tmp_path: Path) -> None:
    async def scenario() -> None:
        workspace_path = tmp_path / "workspace.sqlite3"
        memory_path = tmp_path / "memory.sqlite3"
        workspace = await WorkspaceRepositories.open(workspace_path, workspace=tmp_path)
        memory = await MemoryRepositories.open(memory_path)
        await workspace.close()
        await memory.close()
        connection = sqlite3.connect(workspace_path)
        for table in ("budget_decisions", "model_calls", "routing_decisions"):
            connection.execute(f"DROP TABLE {table}")
        connection.execute("PRAGMA user_version=1")
        connection.commit()
        connection.close()
        connection = sqlite3.connect(memory_path)
        for table in ("embedding_requests", "embedding_consents"):
            connection.execute(f"DROP TABLE {table}")
        connection.execute("PRAGMA user_version=1")
        connection.commit()
        connection.close()
        workspace = await WorkspaceRepositories.open(workspace_path, workspace=tmp_path)
        memory = await MemoryRepositories.open(memory_path)
        try:
            assert await workspace.database.fetch_one(
                "SELECT count(*) FROM model_calls"
            )
            assert await memory.database.fetch_one(
                "SELECT count(*) FROM embedding_consents"
            )
            backups = list((tmp_path / "backups").glob("*.schema-1.*.bak"))
            assert backups and all(
                path.stat().st_mode & 0o777 == 0o600 for path in backups
            )
        finally:
            await workspace.close()
            await memory.close()

    asyncio.run(scenario())


def test_failed_schema_migration_rolls_back_and_preserves_backup(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def scenario() -> None:
        path = tmp_path / "workspace.sqlite3"
        repositories = await WorkspaceRepositories.open(path, workspace=tmp_path)
        await repositories.close()
        connection = sqlite3.connect(path)
        for table in ("budget_decisions", "model_calls", "routing_decisions"):
            connection.execute(f"DROP TABLE {table}")
        connection.execute("PRAGMA user_version=1")
        connection.commit()
        connection.close()
        monkeypatch.setattr(
            WorkspaceDatabase,
            "migrations",
            {1: "CREATE TABLE migration_probe(value INTEGER) STRICT; INVALID SQL;"},
        )
        with pytest.raises(IncompatibleDatabaseError, match="backup preserved"):
            await WorkspaceDatabase.open(path)
        connection = sqlite3.connect(path)
        try:
            assert connection.execute("PRAGMA user_version").fetchone()[0] == 1
            assert (
                connection.execute(
                    "SELECT count(*) FROM sqlite_master WHERE name='migration_probe'"
                ).fetchone()[0]
                == 0
            )
        finally:
            connection.close()
        backups = list((tmp_path / "backups").glob("*.schema-1.*.bak"))
        assert backups and backups[0].stat().st_mode & 0o777 == 0o600

    asyncio.run(scenario())


def test_external_embeddings_require_consent_and_are_audited(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    class Embeddings:
        calls = 0

        async def create(self, *, model, input):
            self.calls += 1
            return SimpleNamespace(
                data=[
                    SimpleNamespace(index=i, embedding=[1.0, float(i)])
                    for i, _ in enumerate(input)
                ],
                usage=SimpleNamespace(prompt_tokens=3),
            )

    async def scenario() -> None:
        monkeypatch.setenv("CAPSLOCK_HOME", str(tmp_path / "home"))
        repositories = await MemoryRepositories.open(tmp_path / "memory.sqlite3")
        api = SimpleNamespace(embeddings=Embeddings())
        service = MemoryService(
            repositories,
            workspace=tmp_path,
            session_id="session",
            external_embedding_profiles={
                "embed": ExternalEmbeddingConfig(
                    "embed", "provider", "embedding-model", "approved", 1.0, api
                )
            },
        )
        try:
            await service.add(
                content="remember this",
                memory_type=MemoryType.FACT,
                scope=MemoryScope.WORKSPACE,
            )
            assert api.embeddings.calls == 0
            preview = await service.external_embedding_preview("embed")
            assert preview["fields"] == ("memory.content", "recall.query")
            assert preview["record_count"] == 1 and preview["byte_count"] > 0
            await service.enable_external_embeddings("embed", preview)
            indexed, failed = await service.rebuild_embeddings()
            assert (indexed, failed) == (1, 0)
            assert api.embeddings.calls == 1
            row = await repositories.database.fetch_one(
                "SELECT operation,status,record_count FROM embedding_requests"
            )
            assert tuple(row) == ("rebuild", "completed", 1)
            await repositories.embedding_audit.revoke(service.workspace_key)
            with pytest.raises(ValueError, match="consent"):
                await service.embeddings.semantic_ranks("query")
        finally:
            await repositories.close()

    asyncio.run(scenario())
