"""Lock INV-WRITER-001: single-writer authority over a candidate worktree.

INV-WRITER-001 (CONVERGE_ORCHESTRATOR_2_0_SOURCE_OF_TRUTH.md:L149 [4. Global invariants]):
At most one candidate writer SHALL have mutation authority over a candidate worktree at any
time, and no model-backed role other than the active autonomous Builder SHALL receive
candidate-write authority.

These deterministic tests pin the three enforcement surfaces:

1. ``WorkspaceOwnershipStore.activate`` refuses a branch that is already owned by another
   path and refuses reactivation once cleanup has been requested.
2. ``opencode_config._role_permission`` grants ``edit = "allow"`` only to the built-in
   ``builder`` role, denies it for every other model-backed role, and fails closed to
   ``deny`` for any unknown role.
3. ``OpenCodeAdapter`` passes ``writable_cwd=True`` only for the builder role and
   ``writable_cwd=False`` for every other model-backed role.

No network, models, or provider calls are made: the sandbox transport is stubbed and every
other code path exercised here is pure/local.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest

from converge_orchestrator.models import AgentConfig, ProjectConfig
from converge_orchestrator.opencode import OpenCodeAdapter
from converge_orchestrator.opencode_config import _ROLE_DEFINITIONS, _role_permission
from converge_orchestrator.workspace_ownership import (
    WorkspaceOwnershipError,
    WorkspaceOwnershipStore,
)

# --- 1. Workspace ownership exclusivity -------------------------------------


def test_activate_refuses_branch_already_owned_by_another_path(tmp_path: Path) -> None:
    """A branch already owned at one path cannot be activated at a second path."""
    store = WorkspaceOwnershipStore(tmp_path)
    first = tmp_path / "candidate-a"
    second = tmp_path / "candidate-b"

    store.activate(task_id="INV-WRITER-001-1", target=first, branch="converge/feature")

    with pytest.raises(WorkspaceOwnershipError, match="already owned by another path"):
        store.activate(task_id="INV-WRITER-001-2", target=second, branch="converge/feature")

    # Authority still points at the original writer; the second writer never took over.
    record = store.read("converge/feature")
    assert record is not None
    assert Path(record.path) == first
    assert record.task_id == "INV-WRITER-001-1"
    assert record.status == "active"


def test_activate_refuses_reactivation_while_cleanup_pending(tmp_path: Path) -> None:
    """Once cleanup is requested, the workspace cannot be reactivated (fail-closed)."""
    store = WorkspaceOwnershipStore(tmp_path)
    target = tmp_path / "candidate-a"
    branch = "converge/feature"

    store.activate(task_id="INV-WRITER-001-1", target=target, branch=branch)
    store.request_cleanup(target=target, branch=branch, reason="crash_recovery")

    with pytest.raises(WorkspaceOwnershipError, match="awaiting cleanup"):
        store.activate(task_id="INV-WRITER-001-2", target=target, branch=branch)

    # The pending-cleanup state is preserved; a second writer cannot slip in.
    record = store.read(branch)
    assert record is not None
    assert record.status == "cleanup_requested"


def test_reactivation_of_active_workspace_at_same_path_is_allowed(tmp_path: Path) -> None:
    """Positive control: an active, non-cleanup workspace may be reactivated in place."""
    store = WorkspaceOwnershipStore(tmp_path)
    target = tmp_path / "candidate-a"
    branch = "converge/feature"

    store.activate(task_id="INV-WRITER-001-1", target=target, branch=branch)
    record = store.activate(task_id="INV-WRITER-001-1", target=target, branch=branch)

    assert record.status == "active"
    assert Path(record.path) == target


# --- 2. Builder-only edit authority across model-backed roles ----------------


def _role_agent() -> AgentConfig:
    return AgentConfig(agent="converge-role")


def test_edit_permission_is_allow_only_for_builder() -> None:
    """Across every built-in role, ``edit`` is ``allow`` only for the builder."""
    agent = _role_agent()
    assert "builder" in _ROLE_DEFINITIONS

    for role in _ROLE_DEFINITIONS:
        expected = "allow" if role == "builder" else "deny"
        assert _role_permission(role, agent)["edit"] == expected, role

    allow_roles = [
        role for role in _ROLE_DEFINITIONS if _role_permission(role, agent)["edit"] == "allow"
    ]
    assert allow_roles == ["builder"]


@pytest.mark.parametrize("unknown_role", ["attacker", "custom_writer", "builder-x", "SUPERUSER"])
def test_unknown_role_fails_closed_to_deny_edit(unknown_role: str) -> None:
    """A role outside the built-in set never receives candidate-write authority."""
    assert _role_permission(unknown_role, _role_agent())["edit"] == "deny"


# --- 3. Adapter passes writable_cwd only to the builder ----------------------


def _config(tmp_path: Path) -> ProjectConfig:
    repo = tmp_path / "repo"
    repo.mkdir(exist_ok=True)
    requirements = tmp_path / "architecture.md"
    requirements.write_text("System must remain correct.\n", encoding="utf-8")
    return ProjectConfig(
        repo_path=repo,
        requirements_path=requirements,
        require_spec_read_only=False,
        agents={
            "builder": AgentConfig(agent="converge-builder"),
            "planner": AgentConfig(agent="converge-planner"),
            "scout": AgentConfig(agent="converge-scout"),
        },
    )


def test_adapter_writable_cwd_true_only_for_builder(tmp_path: Path) -> None:
    """OpenCodeAdapter grants a writable cwd to the builder and denies it elsewhere."""
    cfg = _config(tmp_path)
    adapter = OpenCodeAdapter(cfg)
    fake_result = type("Result", (), {"returncode": 0, "stdout": "ok"})()
    target = "converge_orchestrator.opencode.ExecutionSandbox.run"

    with patch(target, return_value=fake_result) as runner:
        builder = adapter.invoke("builder", "Implement the task", cfg.repo_path)
    assert builder.ok
    assert runner.call_args.kwargs["writable_cwd"] is True

    for role in ("planner", "scout"):
        with patch(target, return_value=fake_result) as runner:
            result = adapter.invoke(role, "Read-only work", cfg.repo_path)
        assert result.ok
        assert runner.call_args.kwargs["writable_cwd"] is False, role
