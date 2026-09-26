"""Versioned local stdio JSON-RPC interface to the existing AgentSession kernel."""

from __future__ import annotations

import asyncio
import json
import sys
from typing import Any

from .domain import ApprovalChoice
from .application.events import event_record
from .application.foreground import ForegroundRunController, ControllerEventKind
from .storage.ownership import WorkspaceBusyError
from .storage.repositories.workflow_records import event as stored_event


class AppServer:
    """One attached session per local client, with durable request deduplication."""

    def __init__(self, application_factory, emit):
        self.factory = application_factory
        self.emit = emit
        self.application = None
        self.initialized = False
        self.subscribed = False
        self.controller: ForegroundRunController | None = None
        self.approvals: dict[str, asyncio.Future] = {}
        self.closed = False
        self._dispatch_lock = asyncio.Lock()
        self._shutdown_task = None

    async def handle(self, message: Any) -> dict | None:
        identifier = message.get("id") if isinstance(message, dict) else None
        if (
            not isinstance(message, dict)
            or message.get("jsonrpc") != "2.0"
            or not isinstance(message.get("method"), str)
            or not isinstance(message.get("params", {}), dict)
            or isinstance(identifier, (dict, list, bool))
        ):
            return self._error(None, -32600, "invalid JSON-RPC request")
        notification = "id" not in message
        try:
            async with self._dispatch_lock:
                if message["method"] != "initialize" and not self.initialized:
                    return (
                        None
                        if notification
                        else self._error(identifier, -32001, "initialize first")
                    )
                result = await self._dispatch(
                    message["method"], message.get("params", {})
                )
            return (
                None
                if notification
                else {"jsonrpc": "2.0", "id": identifier, "result": result}
            )
        except WorkspaceBusyError as exc:
            return None if notification else self._error(identifier, -32002, str(exc))
        except (ValueError, KeyError, TypeError) as exc:
            return None if notification else self._error(identifier, -32602, str(exc))
        except LookupError as exc:
            return None if notification else self._error(identifier, -32601, str(exc))
        except Exception as exc:
            print(f"app-server request failed: {type(exc).__name__}", file=sys.stderr)
            return (
                None
                if notification
                else self._error(identifier, -32603, "internal error")
            )

    @staticmethod
    def _error(identifier, code, message):
        return {
            "jsonrpc": "2.0",
            "id": identifier,
            "error": {"code": code, "message": message},
        }

    @staticmethod
    def _text(params, name):
        value = params.get(name)
        if not isinstance(value, str) or not value.strip() or len(value) > 100_000:
            raise ValueError(f"{name} must be a nonempty bounded string")
        return value

    def _application(self, params):
        if self.application is None:
            raise ValueError("create or resume a session first")
        if params.get("session_id") != self.application.session.session_id:
            raise ValueError("session is not attached")
        return self.application

    async def _dispatch(self, method, params):
        if self.closed:
            raise ValueError("server is closed")
        if method == "initialize":
            if (
                type(params.get("protocol_version")) is not int
                or params["protocol_version"] != 1
            ):
                raise ValueError("supported protocol_version is 1")
            self.initialized = True
            return {
                "protocol_version": 1,
                "transport": "stdio",
                "capabilities": [
                    "session/create",
                    "session/resume",
                    "run/start",
                    "run/cancel",
                    "events/subscribe",
                    "approval/answer",
                    "input/answer",
                ],
                "attached_sessions": 1,
            }
        if method in {"session/create", "session/resume"}:
            session_id = (
                self._text(params, "session_id") if method == "session/resume" else None
            )
            if (
                self.application is not None
                and self.application.session.session_id == session_id
            ):
                return {"session_id": session_id}
            if self.controller is not None and self.controller.busy:
                raise ValueError("cannot change attached session while a run is active")
            if self.application is not None:
                await self.controller.shutdown()
                await self.application.close()
                self.application = None
                self.controller = None
            self.application = await self.factory(session_id)
            self.application.session.set_action_authorizer(self._authorize)
            self.controller = ForegroundRunController(
                self.application.session,
                consumer=self._consume,
                delete_empty_session=False,
            )
            self.subscribed = False
            return {"session_id": self.application.session.session_id}
        if method not in {
            "run/start",
            "run/cancel",
            "events/subscribe",
            "approval/answer",
            "input/answer",
        }:
            raise LookupError(f"unknown method: {method}")
        application = self._application(params)
        if method == "run/start":
            question = self._text(params, "question").strip()
            request_id = self._text(params, "request_id")
            item, created = await application.repositories.work_items.reserve_request(
                application.session.session_id, request_id, question
            )
            if created or (
                item.current_run_id is None
                and str(item.status) == "queued"
                and not self.controller.contains(item.id)
            ):
                await self.controller.enqueue_item(item.id, item.question)
            return {
                "work_item_id": item.id,
                "run_id": item.current_run_id,
                "duplicate": not created,
            }
        if method == "events/subscribe":
            run_id = params.get("run_id")
            if run_id is None:
                self.subscribed = True
                return {
                    "subscribed": True,
                    "events": [],
                    "has_more": False,
                    "next_sequence": 0,
                }
            await application.repositories.runs.require(
                run_id, session_id=application.session.session_id
            )
            after = params.get("after_sequence", 0)
            if type(after) is not int or after < 0:
                raise ValueError("after_sequence must be a nonnegative integer")
            limit = params.get("limit", 100)
            if type(limit) is not int or not 1 <= limit <= 1000:
                raise ValueError("limit must be an integer from 1 to 1000")
            rows = await application.repositories.database.fetch_all(
                "SELECT e.*,r.session_id,r.work_item_id FROM run_events e JOIN runs r ON r.id=e.run_id WHERE e.run_id=? AND e.sequence>? ORDER BY e.sequence LIMIT ?",
                (run_id, after, limit + 1),
            )
            events = [stored_event(row) for row in rows[:limit]]
            self.subscribed = True
            return {
                "subscribed": True,
                "events": [event_record(event) for event in events],
                "has_more": len(rows) > limit,
                "next_sequence": events[-1].sequence if events else after,
            }
        if method == "run/cancel":
            item = await self._requested_item(application, params)
            cancelled = await self.controller.cancel(item.id)
            if cancelled:
                refreshed = await application.repositories.work_items.require(item.id)
                if str(refreshed.status) == "queued":
                    await application.session.cancel_queued_work_item(item.id)
            return {"cancelled": cancelled}
        if method == "approval/answer":
            identifier = self._text(params, "request_id")
            pending_plans = (
                await application.session.plan_requests()
                if hasattr(application.session, "plan_requests")
                else []
            )
            plan_request = next(
                (item for item in pending_plans if item.id == identifier), None
            )
            if plan_request is not None:
                feedback = params.get("feedback")
                if feedback is not None and not isinstance(feedback, str):
                    raise ValueError("feedback must be text")
                decision = await application.session.decide_plan_request(
                    identifier, self._text(params, "choice"), feedback=feedback
                )
                if decision.run_id is None:
                    return {"status": str(decision.status), "run_id": None}
                return await self._resume(application, decision.run_id)
            choice = ApprovalChoice(
                "approve_once"
                if params.get("choice") == "approve"
                else params.get("choice")
            )
            future = self.approvals.get(identifier)
            if future is not None and not future.done():
                future.set_result(choice)
                return {"status": "answered"}
            result = await application.session.decide_permission_request(
                identifier, choice
            )
        else:
            result = await application.repositories.run_journal.answer_input_request(
                self._text(params, "request_id"),
                application.session.session_id,
                params.get("answers"),
            )
        return await self._resume(application, str(result["run_id"]))

    async def _resume(self, application, run_id):
        run = await application.repositories.runs.require(
            run_id, session_id=application.session.session_id
        )
        await self.controller.wait_item(run.work_item_id)
        await self.controller.enqueue_item(run.work_item_id, run.question, run.id)
        return {"status": "answered", "run_id": run_id}

    async def _requested_item(self, application, params):
        if params.get("run_id"):
            run = await application.repositories.runs.require(
                params["run_id"], session_id=application.session.session_id
            )
            item = await application.repositories.work_items.require(run.work_item_id)
        else:
            item = await application.repositories.work_items.require(
                self._text(params, "work_item_id")
            )
        if item.session_id != application.session.session_id:
            raise ValueError("work item is outside attached session")
        return item

    async def _consume(self, notification):
        if self._shutdown_task is not None:
            return
        try:
            if notification.kind is ControllerEventKind.RUN_EVENT and self.subscribed:
                await self.emit(
                    {
                        "jsonrpc": "2.0",
                        "method": "run/event",
                        "params": event_record(notification.event),
                    }
                )
            elif notification.kind is ControllerEventKind.FAILED:
                print(
                    "app-server run failed; inspect durable run events", file=sys.stderr
                )
                await self.emit(
                    {
                        "jsonrpc": "2.0",
                        "method": "run/error",
                        "params": {
                            "work_item_id": notification.work_item_id,
                            "error": "run failed; inspect durable run events",
                        },
                    }
                )
        except (BrokenPipeError, ConnectionError):
            if self._shutdown_task is None:
                self._shutdown_task = asyncio.create_task(
                    self.close(), name="app-server-disconnect"
                )
            raise

    async def _authorize(self, action):
        future = asyncio.get_running_loop().create_future()
        self.approvals[action.id] = future
        try:
            await self.emit(
                {
                    "jsonrpc": "2.0",
                    "method": "approval/request",
                    "params": {
                        "request_id": action.id,
                        "session_id": action.session_id,
                        "run_id": action.run_id,
                        "summary": action.summary,
                        "type": action.type.value,
                    },
                }
            )
            return await future
        finally:
            self.approvals.pop(action.id, None)

    async def wait_idle(self):
        if self.controller is not None:
            await self.controller.wait_idle()

    async def close(self):
        if self.closed:
            return
        self.closed = True
        try:
            if self.controller is not None:
                await self.controller.shutdown()
        finally:
            if self.application is not None:
                self.application.session.set_action_authorizer(None)
                await self.application.close()


async def serve_stdio(workspace, settings, layout) -> int:
    """Serve newline-delimited JSON-RPC until EOF, then cancel and close everything."""
    from .composition.factory import create_application

    async def factory(session_id=None):
        return await create_application(
            workspace, settings, session_id=session_id, layout=layout
        )

    async def emit(message):
        sys.stdout.write(json.dumps(message, ensure_ascii=False) + "\n")
        sys.stdout.flush()

    server = AppServer(factory, emit)
    try:
        while line := await asyncio.to_thread(sys.stdin.readline, 1024 * 1024 + 1):
            if len(line.encode("utf-8")) > 1024 * 1024:
                await emit(server._error(None, -32600, "message exceeds 1 MiB"))
                break
            try:
                message = json.loads(line)
            except (json.JSONDecodeError, UnicodeError):
                await emit(server._error(None, -32700, "invalid JSON"))
                continue
            response = await server.handle(message)
            if response is not None:
                await emit(response)
    finally:
        await server.close()
    return 0
