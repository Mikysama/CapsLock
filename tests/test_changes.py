from pathlib import Path

import pytest

from capslock.changes import ChangeService
from capslock.policy import PolicyError, WorkspacePolicy
from capslock.session import SessionStore
from capslock.tools import RunContext, workspace_tools


def service(tmp_path: Path, session_id: str = "session", run_id: str = "run") -> ChangeService:
    return ChangeService(SessionStore(tmp_path / ".capslock" / "capslock.sqlite3"), WorkspacePolicy(tmp_path), session_id, run_id, lambda *args, **kwargs: None)


def test_edit_proposal_requires_approval_then_applies_and_undoes(tmp_path: Path) -> None:
    target = tmp_path / "note.txt"
    target.write_text("before\n", encoding="utf-8")
    changes = service(tmp_path)
    proposal = changes.propose_edit("note.txt", "before", "after", "Update note")
    assert target.read_text(encoding="utf-8") == "before\n"
    assert proposal.status == "pending" and "-before" in proposal.diff and "+after" in proposal.diff
    with pytest.raises(ValueError, match="explicit approval"):
        changes.apply(proposal.id)
    changes.approve(proposal.id)
    changes.apply(proposal.id)
    assert target.read_text(encoding="utf-8") == "after\n"
    changes.undo_last()
    assert target.read_text(encoding="utf-8") == "before\n"


def test_rejected_or_changed_proposal_has_no_write_side_effect(tmp_path: Path) -> None:
    target = tmp_path / "note.txt"
    target.write_text("before", encoding="utf-8")
    changes = service(tmp_path)
    rejected = changes.propose_edit("note.txt", "before", "after", "Update")
    changes.reject(rejected.id)
    assert target.read_text(encoding="utf-8") == "before"
    changed = changes.propose_edit("note.txt", "before", "after", "Update")
    changes.approve(changed.id)
    target.write_text("external edit", encoding="utf-8")
    with pytest.raises(ValueError, match="changed after proposal"):
        changes.apply(changed.id)
    assert target.read_text(encoding="utf-8") == "external edit"


def test_create_and_session_isolation(tmp_path: Path) -> None:
    first = service(tmp_path, "one")
    proposal = first.propose_create("new.md", "# New\n", "Create note")
    restarted = service(tmp_path, "one")
    restarted.approve(proposal.id)
    restarted.apply(proposal.id)
    assert (tmp_path / "new.md").read_text(encoding="utf-8") == "# New\n"
    with pytest.raises(PolicyError, match="does not belong"):
        service(tmp_path, "two").approve(proposal.id)


def test_write_policy_rejects_internal_binary_and_symlink_escape(tmp_path: Path) -> None:
    (tmp_path / ".git").mkdir()
    (tmp_path / "data.bin").write_bytes(b"\x00")
    outside = tmp_path.parent / "outside.txt"
    outside.write_text("secret", encoding="utf-8")
    (tmp_path / "escape.txt").symlink_to(outside)
    policy = WorkspacePolicy(tmp_path)
    for path in (".git/config", "data.bin", "escape.txt"):
        with pytest.raises(PolicyError):
            policy.writable_file(path, create=path == ".git/config")


def test_model_apply_tool_cannot_bypass_approval(tmp_path: Path) -> None:
    target = tmp_path / "note.txt"
    target.write_text("before", encoding="utf-8")
    store = SessionStore(tmp_path / ".capslock" / "capslock.sqlite3")
    context = RunContext(session_id="session", run_id="run", policy=WorkspacePolicy(tmp_path), event=lambda *args, **kwargs: None, store=store)
    registry = workspace_tools()
    proposed, _ = registry.invoke("propose_file_edit", context, {"path": "note.txt", "old_text": "before", "new_text": "after"})
    change_id = proposed.data["change_id"]
    applied, _ = registry.invoke("apply_change", context, {"change_id": change_id})
    assert not applied.ok and "explicit approval" in applied.error
    assert target.read_text(encoding="utf-8") == "before"
