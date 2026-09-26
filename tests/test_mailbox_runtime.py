import asyncio

from capslock.runtime.tool_loop import ToolLoop
from capslock.tooling.executor import ToolRuntime
from capslock.storage.repositories import WorkspaceRepositories
from tests.helpers import FakeChatModel, answer, workspace_run
from tests.test_runtime import context_factory


def test_message_arriving_during_final_answer_is_processed_before_completion(tmp_path):
    async def scenario():
        repos = await WorkspaceRepositories.open(
            tmp_path / "runtime.sqlite3", workspace=tmp_path
        )
        try:
            session, prepared = await workspace_run(repos)
            model = FakeChatModel(
                answer("first answer"), answer("answer incorporating reply")
            )

            class Receiver:
                def __init__(self):
                    self.finishes = 0

                async def poll(self, messages):
                    return 0

                async def confirm_manual(self, messages):
                    return None

                async def try_finish(self, messages):
                    self.finishes += 1
                    if self.finishes == 1:
                        messages.append(
                            {"role": "user", "content": "Untrusted agent question"}
                        )
                        return False
                    return True

            async def emit(*args):
                return None

            loop = ToolLoop(
                chat_model=model,
                model="test-model",
                tools=ToolRuntime([]),
                journal=repos.run_journal,
                max_tool_rounds=3,
                context_factory=context_factory(repos, session.id),
            )
            result = await loop.run(
                [{"role": "user", "content": "goal"}],
                prepared.run.id,
                emit=emit,
                mailbox_receiver=Receiver(),
            )
            assert result.text == "answer incorporating reply"
            assert len(model.requests) == 2
            assert any(
                m["content"] == "first answer" for m in model.requests[1]["messages"]
            )
        finally:
            await repos.close()

    asyncio.run(scenario())


def test_parent_wait_child_question_reply_finishes_without_manual_parent_read(tmp_path):
    async def scenario():
        from capslock.collaboration import (
            CollaborationService,
            AgentWorkspaceManager,
            MailboxMessageKind,
        )
        from capslock.storage.repositories.mailbox import MailboxDeliveryRepository
        from capslock.runtime.model import (
            ModelResponse,
            ModelMessage,
            ModelToolCall,
            ModelUsage,
        )
        from capslock.tooling.tools.collaboration import (
            delegation_tool,
            agent_control_tools,
        )
        from capslock.runtime.engine import RunRequest
        from capslock.domain import RunLimits, AgentEventKind
        from tests.test_runtime import make_agent
        import json

        repos = await WorkspaceRepositories.open(
            tmp_path / "parent.sqlite3", workspace=tmp_path
        )
        try:
            session = await repos.sessions.create("test-model")
            child_received = []

            async def child(contract, snapshot):
                address = await service.mailbox_address(contract.task_id)
                await service.register_mailbox_runtime(address)
                before = await service.mailbox_snapshot(address)
                question = await service.send_child_message(
                    contract.task_id,
                    parent_run_id=contract.parent_run_id,
                    kind=MailboxMessageKind.QUESTION,
                    payload={"text": "which module?"},
                )
                await service.wait_for_mailbox_since(
                    address, before, timeout=5, actionable_only=True
                )
                values = await service.read_child_messages(
                    contract.task_id, parent_run_id=contract.parent_run_id
                )
                child_received.extend(values)
                assert values[0]["reply_to_message_id"] == question["id"]
                await service.unregister_mailbox_runtime(address)
                return {
                    "summary": "inspected chosen module",
                    "evidence": [],
                    "artifacts": [],
                    "checks": [],
                    "memory_proposals": None,
                }

            service = CollaborationService(
                workspace_manager=AgentWorkspaceManager(tmp_path),
                repository=repos.collaboration,
                child_runner=child,
            )

            class Model:
                def __init__(self):
                    self.calls = 0

                async def complete(self, **request):
                    self.calls += 1
                    if self.calls == 1:
                        call = ModelToolCall(
                            "delegate",
                            "delegate_agents",
                            json.dumps({"tasks": [{"objective": "inspect module"}]}),
                        )
                        return ModelResponse(
                            ModelMessage("", tool_calls=(call,)), ModelUsage(1, 1)
                        )
                    if self.calls == 2:
                        incoming = [
                            m
                            for m in request["messages"]
                            if "which module?" in str(m.get("content"))
                        ]
                        assert incoming
                        envelope = json.loads(incoming[-1]["content"].split("\n", 1)[1])
                        call = ModelToolCall(
                            "reply",
                            "send_agent_message",
                            json.dumps(
                                {
                                    "target_type": "task",
                                    "target_id": envelope["task_id"],
                                    "kind": "response",
                                    "payload": {"text": "auth.py"},
                                    "reply_to_message_id": envelope["id"],
                                }
                            ),
                        )
                        return ModelResponse(
                            ModelMessage("", tool_calls=(call,)), ModelUsage(1, 1)
                        )
                    return answer("done")

            model = Model()
            tools = ToolRuntime([delegation_tool(), *agent_control_tools()])
            agent = make_agent(tmp_path, repos, session.id, model, tools=tools)
            agent.collaboration = service
            agent._mailbox_service = service
            agent.mailbox_receipts = MailboxDeliveryRepository(repos.database)
            events = []
            async with asyncio.timeout(15):
                async for event in agent.run_stream(
                    RunRequest(question="inspect", limits=RunLimits(max_tool_rounds=20))
                ):
                    events.append(event)
            assert events[-1].kind is AgentEventKind.COMPLETED
            assert child_received[0]["payload"]["text"] == "auth.py"
            assert model.calls <= 4
        finally:
            await repos.close()

    asyncio.run(scenario())
