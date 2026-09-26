import asyncio
import tomllib
from pathlib import Path


def test_workspace_preset_is_reversible_and_keeps_other_rules(tmp_path: Path):
    from capslock.tooling.permission_policy.presets import write_workspace_edit_preset

    path = tmp_path / "permissions.toml"
    path.write_text(
        'permissions_version=2\n[[rules]]\nid="mine"\nbehavior="deny"\ntool="shell"\n'
    )
    write_workspace_edit_preset(path, enabled=True)
    document = tomllib.loads(path.read_text())
    assert {r["tool"] for r in document["rules"] if r["behavior"] == "allow"} == {
        "create_file",
        "edit_file",
        "write_file",
        "edit_notebook",
    }
    write_workspace_edit_preset(path, enabled=True)
    assert len(tomllib.loads(path.read_text())["rules"]) == 5
    write_workspace_edit_preset(path, enabled=False)
    assert [r["id"] for r in tomllib.loads(path.read_text())["rules"]] == ["mine"]


def test_workspace_preset_denial_does_not_write(tmp_path: Path):
    from types import SimpleNamespace
    from capslock.cli.permission_presets import workspace_edit_preset

    class UI:
        async def confirm(self, *args, **kwargs):
            return False

    context = SimpleNamespace(
        ui=UI(),
        session=SimpleNamespace(
            permission_engine=SimpleNamespace(
                paths=[("local", tmp_path / "permissions.toml")]
            )
        ),
    )
    asyncio.run(workspace_edit_preset(context, enabled=True))
    assert not (tmp_path / "permissions.toml").exists()
