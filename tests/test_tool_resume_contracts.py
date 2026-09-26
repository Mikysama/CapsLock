"""Fresh and resumed calls share authorization, execution and result boundaries."""

import asyncio
from dataclasses import replace

import pytest

from capslock.policy import InvalidPathError, PolicyError, WorkspacePolicy
from capslock.tooling.contracts import (
    ExecutionContext,
    InterruptBehavior,
    ResolvedToolPolicy,
    ToolExecutionState,
    ToolOutcome,
    ToolOutcomeStatus,
    ToolPause,
    define_tool,
)
from capslock.tooling.executor import ToolRuntime
from capslock.tooling.schema import SchemaValidationError


def _context(path):
    return ExecutionContext(
        session_id="s",
        run_id="r",
        actions=object(),
        policy=WorkspacePolicy(path),
        event=lambda *args, **kwargs: None,
    )


async def _call(runtime, context, *, resumed, arguments=None):
    if resumed:
        return await runtime.resume(
            "example",
            context,
            arguments or {},
            ToolPause("user_input", "input1", {}),
            {"answer": "yes"},
        )
    return await runtime.invoke("example", context, arguments or {})


@pytest.mark.parametrize("resumed", [False, True])
def test_authorization_blocks_handler_on_both_paths(tmp_path, resumed):
    calls = []

    class Middleware:
        async def normalize(self, tool, arguments, context):
            return arguments

        async def authorize(self, *args):
            return ToolOutcome(
                ToolOutcomeStatus.DENIED, False, error_code="permission_denied"
            )

        async def after(self, tool, arguments, policy, outcome, context):
            return outcome

    async def execute(*args):
        calls.append("executed")
        return ToolOutcome.success({"value": "ok"})

    runtime = ToolRuntime(
        [
            define_tool(
                "example", "Example.", {"type": "object"}, execute, resume=execute
            )
        ],
        middleware=(Middleware(),),
    )
    result = asyncio.run(_call(runtime, _context(tmp_path), resumed=resumed))
    assert result.outcome.status is ToolOutcomeStatus.DENIED
    assert calls == []


@pytest.mark.parametrize("resumed", [False, True])
def test_pipeline_normalizes_validates_authorizes_and_runs_after_once(
    tmp_path, resumed
):
    phases = []

    class Middleware:
        async def normalize(self, tool, arguments, context):
            phases.append("normalize")
            assert "optional" not in arguments
            return arguments

        async def pre_authorize(self, *args):
            phases.append("pre_authorize")

        async def authorize(self, *args):
            phases.append("authorize")

        async def after(self, tool, arguments, policy, outcome, context):
            phases.append("after")
            return replace(outcome, data={**outcome.data, "processed": True})

    async def validate(*args):
        phases.append("validate")

    async def policy(*args):
        phases.append("policy")
        return ResolvedToolPolicy()

    async def execute(context, arguments, reporter):
        phases.append("execute")
        return ToolOutcome.success({"value": "ok"})

    async def resume(context, arguments, pause, response, reporter):
        assert pause.request_id == "input1" and response == {"answer": "yes"}
        phases.append("resume")
        return ToolOutcome.success({"value": "ok"})

    runtime = ToolRuntime(
        [
            define_tool(
                "example",
                "Example.",
                {
                    "type": "object",
                    "properties": {"optional": {"type": "string"}},
                },
                execute,
                resume=resume,
                validate=validate,
                policy=policy,
            )
        ],
        middleware=(Middleware(),),
    )
    result = asyncio.run(
        _call(
            runtime, _context(tmp_path), resumed=resumed, arguments={"optional": None}
        )
    )
    assert phases == [
        "normalize",
        "validate",
        "pre_authorize",
        "policy",
        "authorize",
        "resume" if resumed else "execute",
        "after",
    ]
    assert result.outcome.data["processed"] is True
    assert {
        "validation",
        "authorization",
        "execution",
        "total",
    } <= result.timings_ms.keys()


@pytest.mark.parametrize("resumed", [False, True])
def test_output_validation_preserves_committed_side_effect(tmp_path, resumed):
    writes = []

    async def execute(*args):
        writes.append("once")
        return ToolOutcome.success({"value": 123})

    runtime = ToolRuntime(
        [
            define_tool(
                "example",
                "Example.",
                {"type": "object"},
                execute,
                resume=execute,
                policy=ResolvedToolPolicy(external_side_effects=True),
                output_schema={
                    "type": "object",
                    "required": ["value"],
                    "properties": {"value": {"type": "string"}},
                },
            )
        ]
    )
    result = asyncio.run(_call(runtime, _context(tmp_path), resumed=resumed))
    assert result.outcome.error_code == "invalid_tool_output"
    assert result.outcome.executed is True
    assert result.outcome.effective_execution_state is ToolExecutionState.COMMITTED
    assert writes == ["once"]


@pytest.mark.parametrize("resumed", [False, True])
def test_invalid_arguments_return_failure_without_calling_handler(tmp_path, resumed):
    async def execute(*args):
        pytest.fail("invalid input reached handler")

    runtime = ToolRuntime(
        [
            define_tool(
                "example",
                "Example.",
                {"type": "object", "required": ["id"]},
                execute,
                resume=execute,
            )
        ]
    )
    result = asyncio.run(_call(runtime, _context(tmp_path), resumed=resumed))
    assert result.outcome.error_code == "invalid_tool_arguments"
    assert result.outcome.effective_execution_state is ToolExecutionState.NOT_STARTED


@pytest.mark.parametrize("resumed", [False, True])
def test_timeout_after_side_effect_is_unknown_and_not_retryable(tmp_path, resumed):
    async def execute(*args):
        await asyncio.Event().wait()

    runtime = ToolRuntime(
        [
            define_tool(
                "example",
                "Example.",
                {"type": "object"},
                execute,
                resume=execute,
                policy=ResolvedToolPolicy(
                    external_side_effects=True, timeout_seconds=0.01
                ),
            )
        ]
    )

    async def scenario():
        # Bound the regression even when resume ignores the tool timeout.
        async with asyncio.timeout(0.5):
            return await _call(runtime, _context(tmp_path), resumed=resumed)

    result = asyncio.run(scenario())
    assert result.outcome.error_code == "unknown_execution"
    assert result.outcome.data["retryable"] is False


@pytest.mark.parametrize("resumed", [False, True])
def test_completed_interrupt_finishes_handler_and_postprocessing(tmp_path, resumed):
    async def scenario():
        started, release = asyncio.Event(), asyncio.Event()
        completed = []

        async def execute(*args):
            started.set()
            await release.wait()
            completed.append("done")
            return ToolOutcome.success({"done": True})

        runtime = ToolRuntime(
            [
                define_tool(
                    "example",
                    "Example.",
                    {"type": "object"},
                    execute,
                    resume=execute,
                    policy=ResolvedToolPolicy(
                        external_side_effects=True,
                        interrupt_behavior=InterruptBehavior.COMPLETE,
                    ),
                )
            ]
        )
        context = _context(tmp_path)
        task = asyncio.create_task(_call(runtime, context, resumed=resumed))
        await started.wait()
        task.cancel()
        release.set()
        result = await task
        assert result.outcome.ok
        assert context.runtime_state["interrupt_pending"] is True
        assert completed == ["done"]

    asyncio.run(scenario())


@pytest.mark.parametrize("resumed", [False, True])
def test_postprocessing_cannot_return_invalid_success(tmp_path, resumed):
    class Middleware:
        async def normalize(self, tool, arguments, context):
            return arguments

        async def authorize(self, *args):
            return None

        async def after(self, tool, arguments, policy, outcome, context):
            return replace(outcome, data={"value": "wrong"})

    async def execute(*args):
        return ToolOutcome.success({"value": 1})

    runtime = ToolRuntime(
        [
            define_tool(
                "example",
                "Example.",
                {"type": "object"},
                execute,
                resume=execute,
                output_schema={
                    "type": "object",
                    "properties": {"value": {"type": "integer"}},
                },
            )
        ],
        middleware=(Middleware(),),
    )
    result = asyncio.run(_call(runtime, _context(tmp_path), resumed=resumed))
    assert result.outcome.error_code == "invalid_tool_output"


@pytest.mark.parametrize("resumed", [False, True])
@pytest.mark.parametrize(
    "error_type", [SchemaValidationError, InvalidPathError, PolicyError]
)
def test_validation_exception_after_mutation_is_not_argument_repair(
    tmp_path, resumed, error_type
):
    async def execute(*args):
        (tmp_path / "committed.txt").write_text("written")
        raise error_type("file does not exist: internal post-write failure")

    runtime = ToolRuntime(
        [
            define_tool(
                "example",
                "Example.",
                {"type": "object"},
                execute,
                resume=execute,
                policy=ResolvedToolPolicy(external_side_effects=True),
            )
        ]
    )
    result = asyncio.run(_call(runtime, _context(tmp_path), resumed=resumed))
    assert result.outcome.error_code == "unknown_execution"
    assert result.outcome.effective_execution_state is ToolExecutionState.UNKNOWN
    assert result.outcome.data["retryable"] is False


@pytest.mark.parametrize("resumed", [False, True])
def test_after_exception_retains_committed_execution(tmp_path, resumed):
    class Middleware:
        async def normalize(self, tool, arguments, context):
            return arguments

        async def authorize(self, *args):
            return None

        async def after(self, *args):
            raise SchemaValidationError("postprocessing failed")

    async def execute(*args):
        return ToolOutcome.success({"value": 1})

    runtime = ToolRuntime(
        [
            define_tool(
                "example",
                "Example.",
                {"type": "object"},
                execute,
                resume=execute,
                policy=ResolvedToolPolicy(external_side_effects=True),
            )
        ],
        middleware=(Middleware(),),
    )
    result = asyncio.run(_call(runtime, _context(tmp_path), resumed=resumed))
    assert result.outcome.error_code == "tool_postprocessing_failed"
    assert result.outcome.executed is True
    assert result.outcome.effective_execution_state is ToolExecutionState.COMMITTED
    assert result.outcome.data["retryable"] is False


def test_unknown_and_nonresumable_tools_never_execute(tmp_path):
    async def execute(*args):
        pytest.fail("resume may not restart execute")

    runtime = ToolRuntime(
        [define_tool("example", "Example.", {"type": "object"}, execute)]
    )
    outcome = asyncio.run(_call(runtime, _context(tmp_path), resumed=True)).outcome
    assert outcome.error_code == "tool_resume_unsupported"
    outcome = asyncio.run(
        ToolRuntime([]).resume(
            "absent", _context(tmp_path), {}, ToolPause("user_input", "p", {}), {}
        )
    ).outcome
    assert outcome.error_code == "unsupported_tool"


@pytest.mark.parametrize("resumed", [False, True])
@pytest.mark.parametrize("code", ["invalid_path", "tool_timeout", "unknown_execution"])
@pytest.mark.parametrize("enrich_metadata", [False, True])
def test_handler_returned_failure_keeps_original_classification(
    tmp_path, resumed, code, enrich_metadata
):
    outcome = ToolOutcome.failure(
        "handler failure",
        code=code,
        data={"retryable": code == "invalid_path"},
        execution_state=ToolExecutionState.UNKNOWN
        if code == "unknown_execution"
        else ToolExecutionState.NOT_STARTED,
    )

    async def execute(*args):
        return outcome

    class Middleware:
        async def normalize(self, tool, arguments, context):
            return arguments

        async def authorize(self, *args):
            return None

        async def after(self, tool, arguments, policy, result, context):
            return (
                replace(result, audit_data={"recorded": True})
                if enrich_metadata
                else result
            )

    runtime = ToolRuntime(
        [
            define_tool(
                "example", "Example.", {"type": "object"}, execute, resume=execute
            )
        ],
        middleware=(Middleware(),),
    )
    result = asyncio.run(_call(runtime, _context(tmp_path), resumed=resumed))
    assert result.outcome == (
        replace(outcome, audit_data={"recorded": True}) if enrich_metadata else outcome
    )


def test_resume_rechecks_new_permission_denial(tmp_path):
    from capslock.permissions import PermissionMode
    from capslock.tooling.permission_policy.engine import PermissionEngine
    from capslock.tooling.permission_policy.middleware import PermissionMiddleware

    class Rules:
        async def session_permission_rules(self, session_id):
            return [{"behavior": "deny", "tool": "example", "constraints": {}}]

    async def execute(*args):
        pytest.fail("deny added while paused must block resumption")

    engine = PermissionEngine((), Rules())
    runtime = ToolRuntime(
        [
            define_tool(
                "example", "Example.", {"type": "object"}, execute, resume=execute
            )
        ],
        middleware=(PermissionMiddleware(engine),),
    )
    context = replace(_context(tmp_path), permission_mode=PermissionMode.FULL_ACCESS)
    outcome = asyncio.run(_call(runtime, context, resumed=True)).outcome
    assert outcome.error_code == "permission_denied"


def test_resumed_tool_can_pause_again_without_postprocessing(tmp_path):
    async def execute(*args):
        pytest.fail("must call only resume handler")

    async def resume(*args):
        return ToolPause("user_input", "second", {"questions": []})

    runtime = ToolRuntime(
        [
            define_tool(
                "example",
                "Example.",
                {"type": "object"},
                execute,
                resume=resume,
                output_schema={"type": "object", "required": ["answer"]},
            )
        ]
    )
    result = asyncio.run(_call(runtime, _context(tmp_path), resumed=True))
    assert isinstance(result.execution, ToolPause)
    assert result.execution.request_id == "second"


def test_ask_user_resume_works_with_real_plan_and_permission_middleware(tmp_path):
    from capslock.tooling.permission_policy.engine import PermissionEngine
    from capslock.tooling.permission_policy.middleware import PermissionMiddleware
    from capslock.tooling.planning import PlanningBoundaryMiddleware
    from capslock.tooling.tools import workspace_tools

    class Planning:
        async def is_active(self, session_id):
            return True

    runtime = workspace_tools(
        middleware=(
            PlanningBoundaryMiddleware(),
            PermissionMiddleware(PermissionEngine((), object())),
        )
    )
    context = replace(_context(tmp_path), planning=Planning())
    arguments = {
        "questions": [{"id": "mode", "question": "Choose mode", "options": ["A", "B"]}]
    }

    async def scenario():
        first = await runtime.invoke("ask_user", context, arguments)
        assert isinstance(first.execution, ToolPause)
        result = await runtime.resume(
            "ask_user", context, arguments, first.execution, {"mode": "A"}
        )
        assert result.outcome.ok
        assert result.outcome.data == {"answers": {"mode": "A"}}

    asyncio.run(scenario())


@pytest.mark.parametrize("resumed", [False, True])
def test_cancel_interrupt_cleans_up_handler_before_propagating(tmp_path, resumed):
    async def scenario():
        started, cleaned_up = asyncio.Event(), asyncio.Event()

        async def execute(*args):
            started.set()
            try:
                await asyncio.Event().wait()
            finally:
                cleaned_up.set()

        runtime = ToolRuntime(
            [
                define_tool(
                    "example",
                    "Example.",
                    {"type": "object"},
                    execute,
                    resume=execute,
                    policy=ResolvedToolPolicy.safe_read(),
                )
            ]
        )
        task = asyncio.create_task(_call(runtime, _context(tmp_path), resumed=resumed))
        await started.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert cleaned_up.is_set()

    asyncio.run(scenario())
