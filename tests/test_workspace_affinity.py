from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from converge_orchestrator.registry import ControlRegistry
from converge_orchestrator.workspace_identity import (
    WorkspaceAffinityError,
    assert_workspace_affinity,
    workspace_id,
)


def _git_repo(path: Path) -> Path:
    path.mkdir()
    subprocess.run(
        ["git", "init", "-b", "main", str(path)],
        check=True,
        capture_output=True,
        text=True,
    )
    return path


def test_workspace_identity_is_stable_and_untracked(tmp_path: Path) -> None:
    repo = _git_repo(tmp_path / "repo")

    first = workspace_id(repo)
    second = workspace_id(repo)

    assert second == first
    marker = repo / ".git" / "converge-workspace-id"
    assert marker.read_text(encoding="utf-8").strip() == first
    status = subprocess.run(
        ["git", "-C", str(repo), "status", "--porcelain"],
        check=True,
        capture_output=True,
        text=True,
    )
    assert status.stdout == ""


def test_independent_git_workspaces_get_distinct_identities(tmp_path: Path) -> None:
    first = _git_repo(tmp_path / "first")
    second = _git_repo(tmp_path / "second")

    assert workspace_id(first) != workspace_id(second)


def test_registry_project_binding_rejects_different_workspace(tmp_path: Path) -> None:
    registry = ControlRegistry(tmp_path / "control.sqlite")
    config = tmp_path / "project.yaml"

    project = registry.register_project("payments", config, workspace_id="workspace-a")
    assert project["workspace_id"] == "workspace-a"

    same = registry.register_project("payments", config, workspace_id="workspace-a")
    assert same["workspace_id"] == "workspace-a"

    with pytest.raises(ValueError, match="already bound to workspace workspace-a"):
        registry.register_project("payments", config, workspace_id="workspace-b")


def test_legacy_unbound_project_can_be_bound_once(tmp_path: Path) -> None:
    registry = ControlRegistry(tmp_path / "control.sqlite")
    config = tmp_path / "project.yaml"
    legacy = registry.register_project("payments", config)
    assert legacy["workspace_id"] is None

    bound = registry.register_project("payments", config, workspace_id="workspace-a")
    assert bound["workspace_id"] == "workspace-a"

    with pytest.raises(ValueError, match="refusing registration from workspace workspace-b"):
        registry.register_project("payments", config, workspace_id="workspace-b")


def test_affinity_rejects_same_project_from_another_clone(tmp_path: Path) -> None:
    first = _git_repo(tmp_path / "first")
    second = _git_repo(tmp_path / "second")
    project = {"id": "payments", "workspace_id": workspace_id(first)}

    assert assert_workspace_affinity(project, first) == project["workspace_id"]
    with pytest.raises(WorkspaceAffinityError, match="bound to workspace"):
        assert_workspace_affinity(project, second)
