"""Regression coverage for profile authority and explicit context cache contracts."""

from __future__ import annotations

import asyncio
from dataclasses import replace
from types import SimpleNamespace

import pytest

from capslock.bootstrap import WorkspaceApplication
from capslock.composition.providers import create_provider_clients
from capslock.configuration import ContextSettings, Settings
from capslock.configuration.rules import model_routes
from capslock.evaluation.models import PolicyCandidate
from capslock.evaluation.registry import baseline_values
from capslock.evaluation.runtime_probe import _settings_for_candidate
from capslock.runtime import AgentRuntimeError
from capslock.runtime.context import ContextBudgetManager
from capslock.runtime.model import ModelRunContext
from tests.helpers import workspace_run
from tests.test_context_retrieval import CachingCompactions, Summarizer


def test_model_config_tracks_replaced_profile_provider_and_route(tmp_path):
    settings = Settings.load(tmp_path)
    primary = settings.models[settings.routing.reasoning[0]]
    alternate = replace(
        primary,
        name="alternate",
        model="new-model",
        input_cost_per_million=3.0,
        output_cost_per_million=7.0,
    )
    provider = replace(
        settings.providers[primary.provider],
        api_key="new-key",
        base_url="https://new.example/v1",
        timeout_seconds=17.0,
    )
    updated = replace(
        settings,
        models={**settings.models, alternate.name: alternate},
        providers={primary.provider: provider},
        routing=replace(settings.routing, reasoning=(alternate.name,)),
    )
    view = updated.model_config
    assert (view.model, view.api_key, view.base_url, view.timeout_seconds) == (
        "new-model",
        "new-key",
        "https://new.example/v1",
        17.0,
    )
    assert (view.input_cost_per_million, view.output_cost_per_million) == (3.0, 7.0)
    assert settings.model_config.model == primary.model


def test_profiles_sharing_remote_model_have_independent_output_limits():
    _, profiles, _ = model_routes(
        {
            "providers": {
                "local": {
                    "base_url": "https://example.test/v1",
                    "credential": "env:TEST_KEY",
                }
            },
            "models": {
                "large": {
                    "provider": "local",
                    "model": "shared",
                    "max_output_tokens": 4000,
                },
                "small": {
                    "provider": "local",
                    "model": "shared",
                    "max_output_tokens": 500,
                },
            },
            "routing": {"reasoning": ["large"], "fast": ["small"]},
        },
        resolve_credentials=False,
    )
    assert [profiles[name].max_output_tokens for name in ("large", "small")] == [
        4000,
        500,
    ]


def test_candidate_updates_primary_provider_timeout_independent_of_map_order(tmp_path):
    settings = Settings.load(tmp_path)
    profile = settings.models[settings.routing.reasoning[0]]
    primary = settings.providers[profile.provider]
    settings = replace(
        settings,
        providers={
            "unused": replace(primary, name="unused", timeout_seconds=19),
            profile.provider: primary,
        },
    )
    candidate = PolicyCandidate(
        "probe", {**baseline_values(), "providers.timeout_seconds": 90}
    )
    updated, _ = _settings_for_candidate(
        settings, candidate, provider=profile.provider, model="candidate-model"
    )
    assert (updated.model_config.model, updated.model_config.timeout_seconds) == (
        "candidate-model",
        90,
    )
    assert updated.providers["unused"].timeout_seconds == 19


def test_provider_factory_rejects_empty_map_before_allocating_clients(
    tmp_path, monkeypatch
):
    # A stale legacy view used to create an unreachable client when profiles
    # contained no usable provider. Client construction is an external boundary.
    monkeypatch.setenv("CAPSLOCK_API_KEY", "valid-key")
    settings = replace(Settings.load(tmp_path), providers={})
    allocations = []
    monkeypatch.setattr(
        "capslock.composition.providers.AsyncOpenAI",
        lambda **kwargs: allocations.append(kwargs),
    )
    with pytest.raises(AgentRuntimeError, match="provider"):
        create_provider_clients(settings)
    assert allocations == []


def test_bootstrap_requests_keep_profile_limits_for_a_shared_model(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("CAPSLOCK_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("CAPSLOCK_API_KEY", "test-key")
    settings = Settings.load(tmp_path)
    primary = settings.models[settings.routing.reasoning[0]]
    profiles = {
        "large": replace(primary, name="large", max_output_tokens=4000),
        "small": replace(primary, name="small", max_output_tokens=500),
    }
    settings = replace(
        settings,
        models=profiles,
        routing=replace(settings.routing, reasoning=("large",), fast=("small",)),
    )
    requests = []

    class Responses:
        async def create(self, **kwargs):
            requests.append(kwargs)
            return SimpleNamespace(
                output=[], output_text="ok", status="completed", usage=None
            )

    async def scenario():
        application = await WorkspaceApplication.open(
            workspace=tmp_path,
            settings=settings,
            client={primary.provider: SimpleNamespace(responses=Responses())},
            close_client=False,
        )
        try:
            _, prepared = await workspace_run(application.repositories)
            router = application.session.chat_model
            for name in ("large", "small"):
                await router.open_session(
                    ModelRunContext(prepared.run.id, profile_id=name)
                ).complete(model=primary.model, messages=[], tools=[])
            assert [request["max_output_tokens"] for request in requests] == [4000, 500]
        finally:
            await application.close()

    asyncio.run(scenario())


@pytest.mark.parametrize("operation", ["read", "write"])
def test_summary_cache_type_errors_are_not_retried_with_other_signatures(operation):
    class BrokenCache(CachingCompactions):
        async def summary_segment(self, digest, profile, policy=None):
            if operation == "read" and policy is not None:
                raise TypeError("cache decode failed")
            return None

        async def store_summary_segment(self, *, summary_policy_digest=None, **values):
            if operation == "write" and summary_policy_digest is not None:
                raise TypeError("invalid summary_policy_digest encoding")

    manager = ContextBudgetManager(
        sessions=SimpleNamespace(),
        compactions=BrokenCache(),
        settings=ContextSettings(),
        context_window=4000,
        max_output_tokens=500,
        model_profile="test",
        model_name="test",
        tool_schemas=[],
    )
    with pytest.raises(
        TypeError, match="cache decode failed|summary_policy_digest encoding"
    ):
        asyncio.run(
            manager._summarize(
                [{"id": 1, "role": "user", "content": "history"}], Summarizer()
            )
        )
