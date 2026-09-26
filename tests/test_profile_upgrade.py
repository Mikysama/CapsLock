from __future__ import annotations

import asyncio
import sqlite3
from pathlib import Path

from capslock.storage.repositories import WorkspaceRepositories
from capslock.storage.schema import WORKSPACE_SCHEMA, WORKSPACE_APPLICATION_ID


def test_workspace_upgrade_preserves_sessions_and_unknown_usage(tmp_path: Path) -> None:
    path = tmp_path / "workspace.db"
    connection = sqlite3.connect(path)
    connection.executescript(WORKSPACE_SCHEMA)
    # A genuine legacy schema, including legacy rows with no telemetry.
    columns = [row[1] for row in connection.execute("PRAGMA table_info(sessions)")]
    if "model_profile" in columns:
        connection.execute("ALTER TABLE sessions DROP COLUMN model_profile")
    detail_columns = [
        row[1] for row in connection.execute("PRAGMA table_info(model_calls)")
    ]
    for name in (
        "cached_input_tokens",
        "reasoning_tokens",
        "usage_source",
        "request_id",
        "first_token_ms",
        "retry_delay_ms",
        "output_started",
        "price_snapshot_json",
    ):
        if name in detail_columns:
            connection.execute(f"ALTER TABLE model_calls DROP COLUMN {name}")
    connection.execute(f"PRAGMA application_id={WORKSPACE_APPLICATION_ID}")
    connection.execute("PRAGMA user_version=20")
    connection.commit()
    connection.close()

    async def scenario():
        repo = await WorkspaceRepositories.open(path, workspace=tmp_path)
        try:
            version = await repo.database.fetch_one("PRAGMA user_version")
            assert version[0] == 21
            session = await repo.sessions.create("old-model")
            assert session.model_profile is None
            selected = await repo.sessions.set_model_profile(
                session.id, "main", "new-model"
            )
            assert selected.model_profile == "main"
            assert selected.model == "new-model"
        finally:
            await repo.close()
        assert len(list((tmp_path / "backups").glob("capslock-v20-*.sqlite3"))) == 1
        repo = await WorkspaceRepositories.open(path, workspace=tmp_path)
        await repo.close()
        assert len(list((tmp_path / "backups").glob("capslock-v20-*.sqlite3"))) == 1

    asyncio.run(scenario())


def test_session_profile_switch_updates_all_limits(tmp_path: Path) -> None:
    from capslock.configuration import ModelProfileSettings, ProviderSettings
    from tests.test_runtime import make_agent
    from tests.helpers import FakeChatModel

    async def scenario():
        repo = await WorkspaceRepositories.open(
            tmp_path / "profile.db", workspace=tmp_path
        )
        try:
            session = await repo.sessions.create("old")
            agent = make_agent(tmp_path, repo, session.id, FakeChatModel())
            agent.model_profiles = {
                "large": ModelProfileSettings(
                    "large", "p", "new", 200000, 16000, 2.0, 8.0
                )
            }
            agent.model_providers = {
                "p": ProviderSettings(
                    "p",
                    "openai_responses",
                    "https://example.test",
                    "fake",
                    60,
                    "local",
                    strict_tool_calls=True,
                )
            }
            await agent.set_model_profile("large")
            assert agent.model == "new"
            assert agent.context_budget.context_window == 200000
            assert agent.context_budget.max_output_tokens == 16000
            assert agent.input_cost == 2.0
            assert agent.output_cost == 8.0
            assert (await repo.sessions.require(session.id)).model_profile == "large"
        finally:
            await repo.close()

    asyncio.run(scenario())


def test_profile_validation_is_atomic_and_branches_keep_identity(tmp_path):
    from capslock.configuration import ModelProfileSettings, ProviderSettings
    from tests.test_runtime import make_agent
    from tests.helpers import FakeChatModel
    import pytest

    async def scenario():
        repo = await WorkspaceRepositories.open(
            tmp_path / "state.db", workspace=tmp_path
        )
        try:
            session = await repo.sessions.create("shared", model_profile="first")
            agent = make_agent(tmp_path, repo, session.id, FakeChatModel())
            agent.model_profiles = {
                "first": ModelProfileSettings(
                    "first", "a", "shared", 10000, 1000, 1, 2
                ),
                "second": ModelProfileSettings(
                    "second", "b", "shared", 20000, 2000, 3, 4
                ),
                "bad": ModelProfileSettings("bad", "missing", "bad", 10000, 1000, 1, 2),
            }
            agent.model_providers = {
                name: ProviderSettings(
                    name,
                    "openai_responses",
                    "https://example.test",
                    "fake",
                    60,
                    "local",
                    strict_tool_calls=True,
                )
                for name in ("a", "b")
            }
            await agent.set_model_profile("first")
            with pytest.raises(ValueError, match="ambiguous"):
                await agent.set_model("shared")
            with pytest.raises(ValueError, match="credential"):
                await agent.set_model_profile("bad")
            assert (await repo.sessions.require(session.id)).model_profile == "first"
            assert agent.model_profile_id == "first"
            await agent.set_model_profile("second")
            assert agent.context_budget.context_window == 20000
            assert agent.context_budget.cache_identity == "b:second:shared:adaptive"
            assert agent.input_cost == 3
            branch = await repo.sessions.derive(
                session.id, title="Branch", derivation_kind="branch"
            )
            assert branch.model_profile == "second"
            agent._active_runs = 1
            with pytest.raises(ValueError, match="active"):
                await agent.set_model_profile("first")
            assert (await repo.sessions.require(session.id)).model_profile == "second"
        finally:
            await repo.close()

    asyncio.run(scenario())


def test_restored_deleted_or_ambiguous_profile_blocks_new_run(tmp_path):
    from capslock.configuration import ModelProfileSettings, ProviderSettings
    from capslock.runtime import RunRequest
    from tests.test_runtime import make_agent
    from tests.helpers import FakeChatModel
    import pytest

    async def scenario():
        repo = await WorkspaceRepositories.open(
            tmp_path / "restore.db", workspace=tmp_path
        )
        try:
            session = await repo.sessions.create("shared", model_profile="deleted")
            agent = make_agent(tmp_path, repo, session.id, FakeChatModel())
            agent.model_profiles = {
                key: ModelProfileSettings(key, "p", "shared", 10000, 1000, 1, 2)
                for key in ("one", "two")
            }
            agent.model_providers = {
                "p": ProviderSettings(
                    "p",
                    "openai_responses",
                    "https://example.test",
                    "fake",
                    60,
                    "local",
                    strict_tool_calls=True,
                )
            }
            agent.model = "shared"
            agent.model_profile_id = "deleted"
            with pytest.raises(ValueError, match="unavailable"):
                async for _ in agent.run_stream(RunRequest(question="work")):
                    pass
            agent.model_profile_id = None
            with pytest.raises(ValueError, match="ambiguous"):
                async for _ in agent.run_stream(RunRequest(question="work")):
                    pass
            assert (await repo.sessions.require(session.id)).model_profile == "deleted"
            assert (
                await repo.database.fetch_one(
                    "SELECT count(*) FROM runs WHERE session_id=?", (session.id,)
                )
            )[0] == 0
        finally:
            await repo.close()

    asyncio.run(scenario())


def test_profile_without_tokenizer_preserves_context_strategy(tmp_path):
    from dataclasses import replace
    from capslock.configuration import ModelProfileSettings, ProviderSettings
    from tests.test_runtime import make_agent
    from tests.helpers import FakeChatModel

    async def scenario():
        repo = await WorkspaceRepositories.open(
            tmp_path / "tokenizer.db", workspace=tmp_path
        )
        try:
            session = await repo.sessions.create("test")
            agent = make_agent(tmp_path, repo, session.id, FakeChatModel())
            agent.context_budget.settings = replace(
                agent.context_budget.settings, tokenizer="heuristic"
            )
            agent.model_profiles = {
                "selected": ModelProfileSettings(
                    "selected", "p", "test", 10000, 1000, 1, 2
                )
            }
            agent.model_providers = {
                "p": ProviderSettings(
                    "p",
                    "openai_responses",
                    "http://unused",
                    "real-key",
                    10,
                    "local",
                    strict_tool_calls=True,
                )
            }
            await agent.set_model_profile("selected")
            assert agent.context_budget.estimator.strategy == "heuristic"
            assert agent.context_budget.cache_identity == "p:selected:test:heuristic"
        finally:
            await repo.close()

    asyncio.run(scenario())
