"""Regression coverage for model terminal state, metering and retry contracts."""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from email.utils import format_datetime
from types import SimpleNamespace

import pytest

from capslock.domain import ModelRoutingError
from capslock.runtime.model import (
    AsyncOpenAIResponsesModel,
    ModelDelta,
    ModelMessage,
    ModelResponse,
    ModelUsage,
    stream_model_response,
    _usage,
)
from capslock.runtime.routing import _retry_delay


def adapter(events):
    class Responses:
        async def create(self, **kwargs):
            async def stream():
                for event in events:
                    yield SimpleNamespace(**event)

            return stream()

    return AsyncOpenAIResponsesModel(SimpleNamespace(responses=Responses()))


@pytest.mark.parametrize(
    "status,code",
    [("incomplete", "model_incomplete"), ("failed", "model_response_failed")],
)
def test_partial_response_terminal_preserves_usage_and_fails(status, code):
    async def scenario():
        seen = []
        model = adapter(
            [
                {"type": "response.output_text.delta", "delta": "partial"},
                {
                    "type": f"response.{status}",
                    "response": SimpleNamespace(
                        usage=SimpleNamespace(input_tokens=12, output_tokens=3),
                        incomplete_details=SimpleNamespace(reason="max_output_tokens"),
                        error=SimpleNamespace(message="provider failed"),
                    ),
                },
            ]
        )
        with pytest.raises(ModelRoutingError) as caught:
            async for delta in stream_model_response(
                model, model="test", messages=[], tools=[]
            ):
                seen.append(delta)
        assert caught.value.code == code
        assert seen[0].content == "partial"
        assert seen[-1].usage.input_tokens == 12
        assert seen[-1].completion_status == status

    asyncio.run(scenario())


@pytest.mark.parametrize(
    "events", [[], [{"type": "response.output_text.delta", "delta": "partial"}]]
)
def test_stream_without_terminal_is_failure(events):
    async def scenario():
        with pytest.raises(ModelRoutingError) as caught:
            async for _ in stream_model_response(
                adapter(events), model="test", messages=[], tools=[]
            ):
                pass
        assert caught.value.code == "model_stream_incomplete"

    asyncio.run(scenario())


def test_duplicate_terminal_rejected():
    async def scenario():
        events = [{"type": "response.completed", "response": SimpleNamespace()}] * 2
        with pytest.raises(ModelRoutingError) as caught:
            async for _ in stream_model_response(
                adapter(events), model="test", messages=[], tools=[]
            ):
                pass
        assert caught.value.code == "model_stream_incomplete"

    asyncio.run(scenario())


def test_stream_error_event_is_not_success():
    async def scenario():
        with pytest.raises(ModelRoutingError) as caught:
            async for _ in stream_model_response(
                adapter([{"type": "error", "message": "broken"}]),
                model="test",
                messages=[],
                tools=[],
            ):
                pass
        assert caught.value.code == "model_response_failed"

    asyncio.run(scenario())


def test_complete_only_synthesizes_success_terminal():
    class CompleteOnly:
        async def complete(self, **kwargs):
            return ModelResponse(ModelMessage("ok"), ModelUsage(2, 1))

    async def scenario():
        deltas = [
            d
            async for d in stream_model_response(
                CompleteOnly(), model="test", messages=[], tools=[]
            )
        ]
        assert deltas[-1].completion_status == "completed"

    asyncio.run(scenario())


def test_usage_details_do_not_double_count():
    usage = _usage(
        {
            "input_tokens": 100,
            "output_tokens": 40,
            "input_tokens_details": {"cached_tokens": 70},
            "output_tokens_details": {"reasoning_tokens": 20},
        }
    )
    assert (usage.input_tokens, usage.output_tokens) == (100, 40)
    assert usage.cached_input_tokens == 70
    assert usage.reasoning_tokens == 20
    assert usage.source == "provider"
    assert _usage(None).source == "unknown"


def test_retry_after_seconds_not_capped():
    error = RuntimeError("rate limited")
    error.response = SimpleNamespace(headers={"retry-after": "60"})
    assert _retry_delay(error, 1) == 60


def test_retry_after_http_date(monkeypatch):
    timestamp = 1800000000.0
    monkeypatch.setattr("capslock.runtime.routing.time.time", lambda: timestamp)
    error = RuntimeError("rate limited")
    error.response = SimpleNamespace(
        headers={
            "Retry-After": format_datetime(
                datetime.fromtimestamp(timestamp + 60, timezone.utc), usegmt=True
            )
        }
    )
    assert _retry_delay(error, 1) == 60


class MemoryAudit:
    def __init__(self):
        self.calls = []
        self.finished = []

    async def record_decision(self, *args, **kwargs):
        return 1

    async def start_call(self, *args, **kwargs):
        self.calls.append(kwargs)
        return str(len(self.calls))

    async def finish_call(self, call_id, **kwargs):
        self.finished.append(kwargs)

    async def usage(self, run_id):
        return 0, 0, 0.0


def router_for(client, retries=2):
    from capslock.configuration import (
        ModelProfileSettings,
        ProviderSettings,
        RoutingSettings,
    )
    from capslock.runtime.routing import ModelRouter

    profiles = {
        "primary": ModelProfileSettings(
            "primary", "one", "real-model", 10000, 100, 2, 4
        )
    }
    audit = MemoryAudit()
    router = ModelRouter(
        providers={
            "one": ProviderSettings(
                "one",
                "openai_responses",
                "http://unused",
                "key",
                10,
                "shared",
                "env:TEST",
            )
        },
        profiles=profiles,
        routing=RoutingSettings(("primary",), ("primary",), ("primary",), ("primary",)),
        clients={"one": client},
        audit=audit,
        retries=retries,
    )
    return router, audit


def test_router_preserves_incomplete_error_usage_and_never_replays():
    from capslock.runtime.model import ModelRunContext

    async def scenario():
        client = adapter(
            [
                {"type": "response.output_text.delta", "delta": "partial"},
                {
                    "type": "response.incomplete",
                    "response": SimpleNamespace(
                        usage=SimpleNamespace(input_tokens=10, output_tokens=2)
                    ),
                },
            ]
        )
        router, audit = router_for(client)
        with pytest.raises(ModelRoutingError) as caught:
            async for _ in router.open_session(ModelRunContext("run")).stream_complete(
                model="ignored", messages=[], tools=[]
            ):
                pass
        assert caught.value.code == "model_incomplete"
        assert len(audit.calls) == len(audit.finished) == 1
        assert audit.finished[0]["input_tokens"] == 10
        assert audit.finished[0]["error_code"] == "model_incomplete"
        assert audit.finished[0]["output_started"] is True

    asyncio.run(scenario())


def test_router_rejects_missing_terminal_and_audits_failure():
    from capslock.runtime.model import ModelRunContext

    class Stream:
        async def stream_complete(self, **kwargs):
            yield ModelDelta(content="partial")

    async def scenario():
        router, audit = router_for(Stream())
        with pytest.raises(ModelRoutingError) as caught:
            async for _ in router.open_session(ModelRunContext("run")).stream_complete(
                model="ignored", messages=[], tools=[]
            ):
                pass
        assert caught.value.code == "model_stream_incomplete"
        assert audit.finished[0]["error_code"] == "model_stream_incomplete"

    asyncio.run(scenario())


def test_router_nonstream_incomplete_is_not_a_success():
    from capslock.runtime.model import ModelRunContext

    class Client:
        async def complete(self, **kwargs):
            return ModelResponse(
                ModelMessage("partial"),
                ModelUsage(9, 3),
                "incomplete",
                "max_output_tokens",
            )

    async def scenario():
        router, audit = router_for(Client())
        with pytest.raises(ModelRoutingError) as caught:
            await router.open_session(ModelRunContext("run")).complete(
                model="ignored", messages=[], tools=[]
            )
        assert caught.value.code == "model_incomplete"
        assert audit.finished[0]["input_tokens"] == 9
        assert len(audit.calls) == 1

    asyncio.run(scenario())


def test_router_selected_profile_uses_full_configuration_and_keeps_fallback():
    from capslock.runtime.model import ModelRunContext
    from capslock.configuration import ModelProfileSettings

    class Client:
        def __init__(self):
            self.models = []

        async def complete(self, **kwargs):
            self.models.append(kwargs["model"])
            if len(self.models) == 1:
                error = RuntimeError("unavailable")
                error.status_code = 503
                raise error
            return ModelResponse(ModelMessage("ok"), ModelUsage(2, 1))

    async def scenario():
        client = Client()
        router, audit = router_for(client, retries=0)
        router.profiles["selected"] = ModelProfileSettings(
            "selected", "one", "selected-model", 20000, 200, 10, 20
        )
        await router.open_session(
            ModelRunContext("run", profile_id="selected")
        ).complete(model="gemini-2.5-pro", messages=[], tools=[])
        assert client.models == ["selected-model", "real-model"]
        assert [call["profile"] for call in audit.calls] == ["selected", "primary"]

    asyncio.run(scenario())


def test_router_retry_wait_uses_shared_deadline():
    import time
    from capslock.runtime.model import ModelRunContext

    class Client:
        async def complete(self, **kwargs):
            error = RuntimeError("limited")
            error.status_code = 429
            error.response = SimpleNamespace(headers={"retry-after": "60"})
            raise error

    async def scenario():
        router, audit = router_for(Client())
        with pytest.raises(TimeoutError):
            async with asyncio.timeout(0.1):
                await router.open_session(
                    ModelRunContext("run", deadline_monotonic=time.monotonic() + 0.02)
                ).complete(model="ignored", messages=[], tools=[])
        assert len(audit.calls) == 1
        assert audit.finished[0]["retry_delay_ms"] == 60000

    asyncio.run(scenario())


def test_provider_sdk_retries_disabled(monkeypatch):
    from capslock.cli.providers import create_client

    captured = []
    monkeypatch.setattr(
        "capslock.composition.providers.AsyncOpenAI",
        lambda **kwargs: captured.append(kwargs),
    )
    settings = SimpleNamespace(
        model_config=SimpleNamespace(
            api_key="test-key", base_url="http://unused", timeout_seconds=10
        )
    )
    create_client(settings)
    assert captured[-1]["max_retries"] == 0


def test_empty_usage_is_unknown():
    assert _usage({}).source == "unknown"


def test_cancelled_attempt_is_audited():
    from capslock.runtime.model import ModelRunContext

    class Client:
        async def complete(self, **kwargs):
            raise asyncio.CancelledError()

    async def scenario():
        router, audit = router_for(Client())
        with pytest.raises(asyncio.CancelledError):
            await router.open_session(ModelRunContext("run")).complete(
                model="ignored", messages=[], tools=[]
            )
        assert len(audit.finished) == 1
        assert audit.finished[0]["error_message"] == "model call cancelled"

    asyncio.run(scenario())


def test_router_nonstream_error_carries_partial_message():
    from capslock.runtime.model import ModelRunContext

    class Client:
        async def complete(self, **kwargs):
            return ModelResponse(
                ModelMessage("partial"), ModelUsage(9, 3), "incomplete"
            )

    async def scenario():
        router, _ = router_for(Client())
        with pytest.raises(ModelRoutingError) as caught:
            await router.open_session(ModelRunContext("run")).complete(
                model="ignored", messages=[], tools=[]
            )
        assert caught.value.partial_message.content == "partial"
        assert caught.value.usage.input_tokens == 9

    asyncio.run(scenario())


def test_stream_usage_carries_http_request_id():
    class Responses:
        async def create(self, **kwargs):
            class Stream:
                response = SimpleNamespace(headers={"x-request-id": "req-123"})

                async def __aiter__(self):
                    yield SimpleNamespace(
                        type="response.completed", response=SimpleNamespace(usage=None)
                    )

            return Stream()

    async def scenario():
        model = AsyncOpenAIResponsesModel(SimpleNamespace(responses=Responses()))
        deltas = [
            d
            async for d in stream_model_response(
                model, model="test", messages=[], tools=[]
            )
        ]
        assert deltas[-1].usage.request_id == "req-123"

    asyncio.run(scenario())


def test_cached_price_is_discounted_without_double_counting_reasoning():
    from dataclasses import replace
    from capslock.runtime.routing import _cost, _usage_audit

    router, _ = router_for(None)
    profile = replace(router.profiles["primary"], cached_input_cost_per_million=0.5)
    usage = ModelUsage(100, 40, cached_input_tokens=70, reasoning_tokens=20)
    assert _cost(profile, usage) == pytest.approx(
        (30 * 2 + 70 * 0.5 + 40 * 4) / 1_000_000
    )
    assert _usage_audit(profile, usage)["price_snapshot"]["estimated"] is False
    missing_price = replace(profile, cached_input_cost_per_million=None)
    assert _cost(missing_price, usage) == pytest.approx((100 * 2 + 40 * 4) / 1_000_000)
    assert _usage_audit(missing_price, usage)["price_snapshot"]["estimated"] is True


@pytest.mark.parametrize(
    "delta",
    [ModelDelta(reasoning="thinking"), ModelDelta(tool_index=0, tool_arguments="{")],
)
def test_router_never_retries_after_reasoning_or_tool_arguments(delta):
    from capslock.runtime.model import ModelRunContext

    class Client:
        calls = 0

        async def stream_complete(self, **kwargs):
            self.calls += 1
            yield delta
            error = RuntimeError("server went away")
            error.status_code = 503
            raise error

    async def scenario():
        client = Client()
        router, audit = router_for(client)
        with pytest.raises(ModelRoutingError, match="retry suppressed"):
            async for _ in router.open_session(ModelRunContext("run")).stream_complete(
                model="ignored", messages=[], tools=[]
            ):
                pass
        assert client.calls == len(audit.finished) == 1
        assert audit.finished[0]["output_started"] is True

    asyncio.run(scenario())


def test_router_success_records_first_token_latency_and_details():
    from capslock.runtime.model import ModelRunContext

    async def scenario():
        router, audit = router_for(
            adapter(
                [
                    {"type": "response.output_text.delta", "delta": "ok"},
                    {
                        "type": "response.completed",
                        "response": SimpleNamespace(
                            usage={
                                "input_tokens": 100,
                                "output_tokens": 10,
                                "input_tokens_details": {"cached_tokens": 50},
                            },
                            _request_id="req-1",
                        ),
                    },
                ]
            )
        )
        deltas = [
            d
            async for d in router.open_session(ModelRunContext("run")).stream_complete(
                model="ignored", messages=[], tools=[]
            )
        ]
        assert deltas[-1].completion_status == "completed"
        assert audit.finished[0]["first_token_ms"] >= 0
        assert audit.finished[0]["cached_input_tokens"] == 50
        assert audit.finished[0]["request_id"] == "req-1"
        assert audit.finished[0]["usage_source"] == "provider"

    asyncio.run(scenario())


def test_small_window_selected_profile_excluded_before_request():
    from capslock.runtime.model import ModelRunContext
    from dataclasses import replace

    class Client:
        models = []

        async def complete(self, **kwargs):
            self.models.append(kwargs["model"])
            return ModelResponse(ModelMessage("ok"), ModelUsage(1, 1))

    async def scenario():
        client = Client()
        router, _ = router_for(client)
        router.profiles["tiny"] = replace(
            router.profiles["primary"],
            name="tiny",
            model="tiny-model",
            context_window=1,
        )
        await router.open_session(ModelRunContext("run", profile_id="tiny")).complete(
            model="tiny-model", messages=[], tools=[]
        )
        assert client.models == ["real-model"]

    asyncio.run(scenario())


def test_schema_prompt_fallback_never_bypasses_context_eligibility():
    from dataclasses import replace
    from capslock.domain import ModelRole

    router, _ = router_for(None)
    router.profiles["primary"] = replace(router.profiles["primary"], context_window=1)
    candidates, excluded = router._candidates(
        ModelRole.REASONING,
        [],
        [],
        response_format={
            "type": "json_schema",
            "json_schema": {"name": "result", "schema": {"type": "object"}},
        },
    )
    assert not candidates
    assert excluded == [{"profile": "primary", "reason": "context_window"}]
