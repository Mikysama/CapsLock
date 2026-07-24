"""Action coordinator factory and plugin capability broker assembly."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from typing import Any

from ..application.action_system import (
    ActionCoordinator,
    ActionRunState,
    CommandActionHandler,
    CredentialActionHandler,
    FileActionHandler,
    McpActionHandler,
    WebActionHandler,
    WorktreeActionHandler,
    WorkspaceExecutionScope,
    resolve_named_credential,
)
from ..configuration import Settings
from ..domain import ActionRecord, ActionStatus, ActionType
from ..interaction import RunInteraction
from ..layout import ProjectLayout
from ..lsp import LspManager
from ..mcp import McpManager
from ..plugins import PluginProcessClient, PluginRegistry
from ..plugins.broker import BrokerCallbacks
from ..policy import PolicyError
from ..shell import SessionProcessManager
from ..storage.repositories import WorkspaceRepositories


def build_action_factory(
    *,
    settings: Settings,
    repositories: WorkspaceRepositories,
    session_id: str,
    layout: ProjectLayout,
    scope: WorkspaceExecutionScope,
    lsp: LspManager,
    mcp: McpManager,
    plugins: PluginRegistry,
    plugin_client: PluginProcessClient,
    processes: SessionProcessManager,
    interaction: RunInteraction,
    emit: Callable[..., None],
) -> Callable[[str], ActionCoordinator]:
    def actions(run_id: str) -> ActionCoordinator:
        coordinator: ActionCoordinator | None = None

        def broker_callbacks(parent: ActionRecord) -> BrokerCallbacks:
            async def execute_capability(
                action_type: ActionType, payload: dict[str, object]
            ) -> dict[str, object]:
                if coordinator is None:
                    raise PolicyError("plugin capability approval is unavailable")
                action = await coordinator.propose(action_type, **payload)
                if action.status is not ActionStatus.COMPLETED:
                    raise PolicyError(
                        "plugin capability request was not approved and completed"
                    )
                return dict(action.result or {})

            async def workspace_write(params: dict[str, Any]) -> dict[str, object]:
                path, content = params.get("path"), params.get("content")
                if not isinstance(path, str) or not isinstance(content, str):
                    raise PolicyError(
                        "workspace write requires string path and content"
                    )
                target = scope.policy.writable_file(path, create=True)
                payload: dict[str, object] = {
                    "path": path,
                    "summary": f"Plugin {parent.request.get('plugin')} writes {path}",
                }
                if target.exists():
                    payload["replace_content"] = content
                    action_type = ActionType.FILE_EDIT
                else:
                    payload["content"] = content
                    action_type = ActionType.FILE_CREATE
                return await execute_capability(action_type, payload)

            async def network(params: dict[str, Any]) -> dict[str, object]:
                return await execute_capability(
                    ActionType.WEB_FETCH, {"url": params.get("url")}
                )

            async def process(params: dict[str, Any]) -> dict[str, object]:
                return await execute_capability(
                    ActionType.COMMAND,
                    {
                        "template": params.get("template"),
                        "target": params.get("target"),
                        "cwd": params.get("cwd", "."),
                    },
                )

            async def credential(params: dict[str, Any]) -> dict[str, object]:
                name = params.get("name")
                if not isinstance(name, str):
                    raise PolicyError("credential capability requires a name")
                await execute_capability(ActionType.CREDENTIAL_ACCESS, {"name": name})
                secret = await asyncio.to_thread(resolve_named_credential, name)
                return {"name": name, "value": secret}

            return BrokerCallbacks(
                workspace_write=workspace_write,
                network=network,
                process=process,
                credential=credential,
            )

        handlers = [
            FileActionHandler(scope.policy, did_change=lsp.did_change),
            CommandActionHandler(
                scope.policy,
                timeout_seconds=settings.shell.default_timeout_seconds,
                output_limit_bytes=settings.shell.output_bytes,
                max_timeout_seconds=settings.shell.max_timeout_seconds,
                process_manager=processes,
            ),
            WebActionHandler(
                repositories.sources,
                tavily_api_key=settings.web.tavily_api_key,
                timeout_seconds=settings.web.web_timeout_seconds,
                max_bytes=settings.web.web_max_bytes,
                max_redirects=settings.web.web_max_redirects,
            ),
            McpActionHandler(
                scope.policy,
                output_limit_bytes=settings.mcp.mcp_output_bytes,
                plugin_registry=plugins,
                plugin_client=plugin_client,
                broker_callbacks=broker_callbacks,
                mcp_client=mcp,
            ),
            CredentialActionHandler(),
            WorktreeActionHandler(
                database=repositories.database,
                session_id=session_id,
                state_root=layout.root / "state",
                scope=scope,
                max_per_session=settings.worktree.max_per_session,
                process_manager=processes,
            ),
        ]
        coordinator = ActionCoordinator(
            repositories.actions,
            ActionRunState(repositories.runs, repositories.workflow),
            session_id=session_id,
            run_id=run_id,
            handlers=handlers,
            event=emit,
            interaction=interaction,
        )
        return coordinator

    return actions


__all__ = ["build_action_factory"]
