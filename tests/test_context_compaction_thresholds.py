import asyncio
from types import SimpleNamespace

from capslock.configuration import ContextSettings
from capslock.runtime.context import ContextBudgetManager


def _manager(*, tool_schemas=None, trigger_ratio=0.80):
    return ContextBudgetManager(
        sessions=SimpleNamespace(),
        compactions=SimpleNamespace(),
        settings=ContextSettings(trigger_ratio=trigger_ratio),
        context_window=100_000,
        max_output_tokens=10_000,
        model_profile="test",
        model_name="test",
        tool_schemas=tool_schemas or [],
    )


def test_schema_is_counted_in_input_only_once() -> None:
    plain = _manager(trigger_ratio=0.98)
    with_tools = _manager(
        trigger_ratio=0.98,
        tool_schemas=[{"type": "function", "name": "x", "description": "x" * 1000}],
    )

    assert plain.soft_trigger_tokens < plain.input_budget
    assert with_tools.soft_trigger_tokens == plain.soft_trigger_tokens
    assert with_tools.estimate([]) > plain.estimate([])
    assert with_tools.hard_limit_tokens == plain.hard_limit_tokens


def test_compaction_decision_has_soft_hard_and_forced_paths() -> None:
    manager = _manager()
    assert manager.compaction_decision(manager.trigger_tokens).action == "none"
    assert manager.compaction_decision(manager.trigger_tokens + 1).action == "compact"
    assert (
        manager.compaction_decision(manager.hard_limit_tokens + 1).reason
        == "hard_limit_exceeded"
    )
    assert manager.compaction_decision(1, force=True).reason == "forced_recovery"


def test_observed_growth_replaces_percentage_trigger():
    manager = _manager()
    assert manager.trigger_tokens == 72_000
    manager.observe_context_size(10_000)
    manager.observe_context_size(12_000)
    # Unknown provider usage must not enable the dynamic policy.
    assert manager.trigger_tokens == 72_000
    asyncio.run(manager.observe_usage([], [], 12_000))
    assert manager.trigger_tokens == 90_000 - manager.safety_margin_tokens - 2_000
    manager.observe_context_size(42_000)
    assert manager.trigger_tokens == 90_000 - manager.safety_margin_tokens - 30_000
    assert manager.target_tokens < manager.trigger_tokens


def test_compaction_and_profile_change_do_not_fake_growth():
    manager = _manager()
    manager.observe_context_size(10_000)
    manager.observe_context_size(12_000)
    asyncio.run(manager.observe_usage([], [], 12_000))
    manager.observe_context_size(3_000)
    assert manager.expected_growth_tokens == 2_000
    manager.cache_identity = "different-provider"
    manager.observe_context_size(4_000)
    assert manager.trigger_tokens == 72_000


def test_unknown_usage_keeps_fallback_and_disabled_compaction_checks_hard_limit():
    from capslock.runtime.context import ContextBudgetExceeded
    import pytest

    manager = _manager()
    manager.observe_context_size(1_000)
    manager.observe_context_size(2_000)
    asyncio.run(manager.observe_usage([], [], 0))
    assert manager.expected_growth_tokens is None
    assert manager.trigger_tokens == 72_000
    manager.settings = ContextSettings(auto_compact=False)
    with pytest.raises(ContextBudgetExceeded):
        asyncio.run(
            manager.compact_checkpoint(
                [{"role": "user", "content": "too much content " * 100_000}],
                session_id="session",
                run_id="run",
                summarizer=None,
            )
        )


def test_runtime_boundaries_collect_growth_without_model_compression():
    manager = _manager()

    async def scenario():
        messages = [{"role": "user", "content": "goal"}]
        await manager.compact_checkpoint(
            messages, session_id="s", run_id="r", summarizer=None
        )
        before = manager.estimate(messages)
        await manager.observe_usage(messages, [], before)
        messages.append({"role": "assistant", "content": "result " * 300})
        await manager.compact_checkpoint(
            messages, session_id="s", run_id="r", summarizer=None
        )
        assert manager.expected_growth_tokens is not None
        assert manager.expected_growth_tokens > 0
        assert manager.trigger_tokens > 72_000

    asyncio.run(scenario())
