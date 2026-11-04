"""Pin REQ-AGENT-003: builder is the only model-backed writer and is scoped to the
active candidate worktree.

REQ-AGENT-003 (CONVERGE_ORCHESTRATOR_2_0_SOURCE_OF_TRUTH.md:L249
[7. Deterministic architecture and agent roles]): The autonomous Builder SHALL be the only
model-backed writer and SHALL receive write access only to the active candidate worktree and
explicitly authorized scratch locations.

These deterministic offline assertions lock the four enforcement surfaces:

1. Role permissions: only the built-in ``builder`` role receives ``edit = "allow"``.
2. Adapter scope: ``OpenCodeAdapter`` passes ``writable_cwd=True`` only for the builder.
3. Worktree root boundary: ``WorkspaceOwnershipStore`` refuses to activate a workspace whose
   target path escapes the configured worktree root, so the builder's writable cwd is bound
   to the active candidate worktree.
4. External-directory gate: the builder's ``external_directory`` permission is ``deny`` and
   is protected from operator override, preventing writes to arbitrary scratch locations that
   are not explicitly authorized by the orchestrator.

No network, model, provider, git command, subprocess call, or filesystem mutation is
performed.

This module is a requirement-specific pin that complements the broader INV-WRITER-001
single-writer lock in ``tests/test_inv_writer_001_single_writer.py``.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest

from converge_orchestrator.models import AgentConfig, ProjectConfig
from converge_orchestrator.opencode import OpenCodeAdapter
from converge_orchestrator.opencode_config import _ROLE_DEFINITIONS, _role_permission
from converge_orchestrator.workspace_ownership import (
    WorkspaceOwnershipError,
    WorkspaceOwnershipStore,
)

# --- 1. Builder is the only model-backed writer --------------------------------


_BUILT_IN_MODEL_BACKED_ROLES = sorted(_ROLE_DEFINITIONS)


def _permission(role: str, **agent_kwargs: Any) -> dict[str, Any]:
    return _role_permission(role, AgentConfig(agent=f"converge-{role}", **agent_kwargs))


@pytest.mark.parametrize("role", _BUILT_IN_MODEL_BACKED_ROLES)
def test_req_agent_003_only_builder_has_edit_allow(role: str) -> None:
    """Across every built-in model-backed role, edit is allow only for the builder."""
    permission = _permission(role)
    expected = "allow" if role == "builder" else "deny"
    assert permission["edit"] == expected, role



def test_req_agent_003_only_allow_role_is_builder() -> None:
    """The set of roles granted edit=allow collapses to exactly the builder."""
    allow_roles = [
        role for role in _BUILT_IN_MODEL_BACKED_ROLES if _permission(role)["edit"] == "allow"
    ]
    assert allow_roles == ["builder"]


@pytest.mark.parametrize("unknown_role", ["attacker", "custom_writer", "builder-x", "SUPERUSER"])
def test_req_agent_003_unknown_role_fails_closed_to_deny_edit(unknown_role: str) -> None:
    """A role outside the built-in set never receives candidate-write authority."""
    assert _role_permission(unknown_role, AgentConfig(agent="converge-role"))["edit"] == "deny"


# --- 2. Builder prompt asserts sole writer identity and worktree scope -------


def test_req_agent_003_builder_prompt_declares_sole_writer() -> None:
    """The builder prompt carries the sole-writer directive and worktree scope."""
    prompt = _ROLE_DEFINITIONS["builder"]["prompt"]
    assert "sole writer" in prompt
    assert "worktree" in prompt
    assert "Never push" in prompt
    assert "merge" in prompt or "reset" in prompt


# --- 3. OpenCodeAdapter grants writable_cwd only to the builder ----------------


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


def test_req_agent_003_adapter_writable_cwd_true_only_for_builder(tmp_path: Path) -> None:
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


# --- 4. Workspace ownership binds the writer to the active candidate worktree --


def test_req_agent_003_workspace_ownership_refuses_path_outside_worktree_root(
    tmp_path: Path,
) -> None:
    """The ownership store rejects a workspace target that escapes the worktree root."""
    worktree_root = tmp_path / "worktrees"
    worktree_root.mkdir()
    store = WorkspaceOwnershipStore(worktree_root)
    valid_target = worktree_root / "candidate-a"
    invalid_target = tmp_path / "other-root" / "candidate-b"
    invalid_target.parent.mkdir(parents=True, exist_ok=True)

    # Valid direct child of the worktree root is accepted.
    record = store.activate(
        task_id="REQ-AGENT-003-1", target=valid_target, branch="converge/feature-a"
    )
    assert record.status == "active"
    assert Path(record.path) == valid_target

    # A path outside the configured root can never become the active candidate worktree.
    with pytest.raises(WorkspaceOwnershipError, match="outside the configured worktree root"):
        store.activate(
            task_id="REQ-AGENT-003-2", target=invalid_target, branch="converge/feature-b"
        )


# --- 5. Builder cannot be authorized to write to arbitrary external directories


def test_req_agent_003_builder_external_directory_is_deny() -> None:
    """The builder's external_directory permission is deny by default."""
    permission = _permission("builder")
    assert permission["external_directory"] == "deny"


@pytest.mark.parametrize("protected", ("edit", "external_directory"))
def test_req_agent_003_builder_tool_permissions_cannot_weaken_scope(
    protected: str,
) -> None:
    """Operator tool_permissions cannot grant protected write tooling to the builder."""
    with pytest.raises(ValueError, match="custom/MCP tools"):
        _permission("builder", tool_permissions={protected: "allow"})
