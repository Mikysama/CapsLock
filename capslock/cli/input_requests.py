"""Headless management of durable ask_user requests."""

from __future__ import annotations

import json

from ..storage.repositories import WorkspaceRepositories


async def input_command(output, layout, workspace, settings, args) -> int:
    repositories = await WorkspaceRepositories.open(layout.database, workspace=workspace)
    try:
        command = args.input_command
        if command in {None, "list"}:
            rows = await repositories.database.fetch_all(
                """SELECT id,session_id,run_id,questions_json,created_at
                   FROM tool_input_requests WHERE status='pending'
                   ORDER BY created_at,id"""
            )
            for row in rows:
                output.print(
                    json.dumps(
                        {
                            "request_id": str(row["id"]),
                            "session_id": str(row["session_id"]),
                            "run_id": str(row["run_id"]),
                            "questions": json.loads(row["questions_json"]),
                            "created_at": str(row["created_at"]),
                        },
                        ensure_ascii=False,
                    )
                )
            return 0
        row = await repositories.database.fetch_one(
            "SELECT session_id,run_id FROM tool_input_requests WHERE id=?",
            (args.request_id,),
        )
        if row is None:
            raise ValueError("input request does not exist")
        session_id = str(row["session_id"])
        if command == "answer":
            try:
                answers = json.loads(args.answers_json)
            except json.JSONDecodeError as exc:
                raise ValueError("--answers-json must contain valid JSON") from exc
            result = await repositories.run_journal.answer_input_request(
                args.request_id, session_id, answers
            )
        elif command == "cancel":
            result = await repositories.run_journal.cancel_input_request(
                args.request_id, session_id
            )
        else:
            raise ValueError("unknown input command")
        run = await repositories.runs.require(
            str(result.get("run_id") or row["run_id"]), session_id=session_id
        )
        output.print(json.dumps(result, ensure_ascii=False))
    finally:
        await repositories.close()
    from .app import create_application
    from .context import CliContext
    from .exec import run_exec

    application = await create_application(
        workspace, settings, session_id=session_id, layout=layout
    )
    async with application:
        return await run_exec(
            CliContext(output, application.session, application.queries),
            run.question,
            spinner=False,
            resume_from_run_id=run.id,
        )
