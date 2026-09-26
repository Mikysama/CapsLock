"""Slash-command handlers for diagnostics operations."""

from __future__ import annotations

from types import SimpleNamespace

from ..commands import CommandOutcome
from .support import get_repositories, get_ui


async def stats(context, parts: list[str], raw: str) -> CommandOutcome:
    scope = parts[1] if len(parts) == 2 else "workspace"
    if len(parts) > 2 or scope not in {"workspace", "session"}:
        raise ValueError("usage: /stats [workspace|session]")
    repositories = get_repositories(context)
    session_id = context.session.session_id if scope == "session" else None
    usage = await repositories.runs.usage_breakdown(session_id)
    where, values = (" WHERE session_id=?", (session_id,)) if session_id else ("", ())
    sessions = (
        1
        if session_id
        else int(
            (
                await repositories.database.fetch_one(
                    "SELECT count(*) FROM sessions WHERE deletion_state IS NULL"
                )
            )[0]
        )
    )
    tools = await repositories.database.fetch_one(
        "SELECT count(*) FROM tool_calls"
        + (
            " WHERE run_id IN (SELECT id FROM runs WHERE session_id=?)"
            if session_id
            else ""
        ),
        values,
    )
    actions = await repositories.database.fetch_one(
        "SELECT count(*) FROM actions" + where, values
    )
    compactions = await repositories.database.fetch_one(
        "SELECT count(*) FROM context_compactions" + where, values
    )
    statuses = await repositories.database.fetch_all(
        "SELECT status,count(*) AS count FROM runs"
        + where
        + " GROUP BY status ORDER BY status",
        values,
    )
    model_rows = await repositories.database.fetch_all(
        """SELECT mc.model,count(*) AS calls FROM model_calls mc JOIN runs r ON r.id=mc.run_id"""
        + (" WHERE r.session_id=?" if session_id else "")
        + " GROUP BY mc.model ORDER BY calls DESC,mc.model LIMIT 5",
        values,
    )
    tool_rows = await repositories.database.fetch_all(
        """SELECT tc.name,count(*) AS calls FROM tool_calls tc JOIN runs r ON r.id=tc.run_id"""
        + (" WHERE r.session_id=?" if session_id else "")
        + " GROUP BY tc.name ORDER BY calls DESC,tc.name LIMIT 8",
        values,
    )
    child_rows = await repositories.database.fetch_one(
        "SELECT count(*) FROM agent_tasks"
        + (
            " WHERE parent_run_id IN (SELECT id FROM runs WHERE session_id=?)"
            if session_id
            else ""
        ),
        values,
    )
    unknown_row = await repositories.database.fetch_one(
        "SELECT count(*) FROM model_calls mc JOIN runs r ON r.id=mc.run_id WHERE (mc.usage_source IS NULL OR mc.usage_source='unknown')"
        + (" AND r.session_id=?" if session_id else ""),
        values,
    )
    unknown_usage = bool(unknown_row[0])
    main = next((row for row in usage if row["kind"] == "agent"), {})
    maintenance = [
        row for row in usage if row["kind"] in {"local_command", "side_question"}
    ]

    def usage_label(row):
        if unknown_usage:
            return "usage unknown (reported totals are incomplete)"
        return f"tokens {row.get('input_tokens', 0) + row.get('output_tokens', 0)}; cost ${float(row.get('cost_usd', 0)):.6f}"

    lines = [
        f"Sessions: {sessions}",
        f"Agent runs: {main.get('run_count', 0)}; {usage_label(main)}; duration {main.get('duration_ms', 0)} ms",
        f"Run status: {', '.join(f'{r["status"]}={r["count"]}' for r in statuses) or 'none'}",
        f"Tool calls: {int(tools[0])}; Actions: {int(actions[0])}; compactions: {int(compactions[0])}; child Agents: {int(child_rows[0])}",
        f"Models: {', '.join(f'{r["model"]} ({r["calls"]})' for r in model_rows) or 'none'}",
        f"Tools: {', '.join(f'{r["name"]} ({r["calls"]})' for r in tool_rows) or 'none'}",
    ]
    for row in maintenance:
        lines.append(
            f"Maintenance {row['kind']}: {row['run_count']} runs; {usage_label(row)}"
        )
    await get_ui(context).show(f"Stats · {scope}", "\n".join(lines))
    return CommandOutcome()


async def doctor(context, parts: list[str], raw: str) -> CommandOutcome:
    if "--fix" in parts:
        raise ValueError("/doctor --fix is not supported; use `capslock doctor --fix`")
    if any(part not in {"/doctor", "--network"} for part in parts):
        raise ValueError("usage: /doctor [--network]")
    from .diagnostics import doctor as run_doctor

    layout = context.application.layout if context.application else None
    if layout is None:
        raise RuntimeError("doctor requires an application context")
    await run_doctor(
        context.console,
        context.session.workspace,
        layout=layout,
        args=SimpleNamespace(
            fix=False, yes=False, network="--network" in parts, json=False, strict=False
        ),
    )
    return CommandOutcome()
