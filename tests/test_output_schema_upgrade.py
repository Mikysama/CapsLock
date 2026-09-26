import asyncio
import json
from pathlib import Path

import pytest

from capslock.storage.repositories import WorkspaceRepositories
from tests.helpers import FakeChatModel, answer
from tests.test_runtime import make_agent, collect


def test_structured_final_answer_rejects_wrong_type(tmp_path: Path):
    async def scenario():
        repo = await WorkspaceRepositories.open(
            tmp_path / "state.db", workspace=tmp_path
        )
        try:
            session = await repo.sessions.create("test-model")
            agent = make_agent(
                tmp_path, repo, session.id, FakeChatModel(answer('{"count":"wrong"}'))
            )
            fmt = {
                "type": "json_schema",
                "json_schema": {
                    "name": "result",
                    "schema": {
                        "type": "object",
                        "properties": {"count": {"type": "integer"}},
                        "required": ["count"],
                    },
                },
            }
            with pytest.raises(Exception, match="structured output"):
                await collect(agent, "count", response_format=fmt)
            rows = await repo.database.fetch_all("SELECT status,error_code FROM runs")
            assert rows[0]["status"] == "failed"
            assert rows[0]["error_code"] == "output_schema_validation_failed"
        finally:
            await repo.close()

    asyncio.run(scenario())


def test_output_schema_rejects_external_refs_before_execution(tmp_path: Path):
    from capslock.output_schema import load_output_schema

    path = tmp_path / "schema.json"
    path.write_text(json.dumps({"$ref": "https://example.test/schema.json"}))
    with pytest.raises(ValueError, match="reference"):
        load_output_schema(path)
