from __future__ import annotations

import subprocess
from pathlib import Path
from unittest.mock import Mock

import pytest

from converge_orchestrator.registry import ControlRegistry
from converge_orchestrator.runtime_service import ScheduledRunController
from converge_orchestrator.workspace_identity import (
    WorkspaceAffinityError,
    assert_state_store_affinity,
    assert_workspace_affinity,
    state_store_id,
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


def test_state_store_identity_is_stable_and_distinguishes_local_state_dirs(
    tmp_path: Path,
) -> None:
    first = tmp_path / "state-a"
    second = tmp_path / "state-b"

    first_id = state_store_id(first)
    assert state_store_id(first) == first_id
    assert state_store_id(second) != first_id
    assert (first / ".converge-state-id").read_text(encoding="utf-8").strip() == first_id


def test_registry_project_binding_rejects_different_workspace(tmp_path: Path) -> None:
    registry = ControlRegistry(tmp_path / "control.sqlite")
    config = tmp_path / "project.yaml"

    project = registry.register_project("payments", config, workspace_id="workspace-a")
    assert project["workspace_id"] == "workspace-a"

    same = registry.register_project("payments", config, workspace_id="workspace-a")
    assert same["workspace_id"] == "workspace-a"

    with pytest.raises(ValueError, match="already bound to workspace workspace-a"):
        registry.register_project("payments", config, workspace_id="workspace-b")


def test_registry_project_binding_rejects_different_state_store(tmp_path: Path) -> None:
    registry = ControlRegistry(tmp_path / "control.sqlite")
    config = tmp_path / "project.yaml"

    project = registry.register_project(
        "payments",
        config,
        workspace_id="workspace-a",
        state_store_id="state-a",
    )
    assert project["state_store_id"] == "state-a"

    same = registry.register_project(
        "payments",
        config,
        workspace_id="workspace-a",
        state_store_id="state-a",
    )
    assert same["state_store_id"] == "state-a"

    with pytest.raises(ValueError, match="already bound to state store state-a"):
        registry.register_project(
            "payments",
            config,
            workspace_id="workspace-a",
            state_store_id="state-b",
        )


def test_legacy_unbound_project_can_be_bound_once(tmp_path: Path) -> None:
    registry = ControlRegistry(tmp_path / "control.sqlite")
    config = tmp_path / "project.yaml"
    legacy = registry.register_project("payments", config)
    assert legacy["workspace_id"] is None
    assert legacy["state_store_id"] is None

    bound = registry.register_project(
        "payments",
        config,
        workspace_id="workspace-a",
        state_store_id="state-a",
    )
    assert bound["workspace_id"] == "workspace-a"
    assert bound["state_store_id"] == "state-a"

    with pytest.raises(ValueError, match="refusing registration from workspace workspace-b"):
        registry.register_project(
            "payments",
            config,
            workspace_id="workspace-b",
            state_store_id="state-a",
        )


def test_affinity_rejects_same_project_from_another_clone(tmp_path: Path) -> None:
    first = _git_repo(tmp_path / "first")
    second = _git_repo(tmp_path / "second")
    project = {"id": "payments", "workspace_id": workspace_id(first)}

    assert assert_workspace_affinity(project, first) == project["workspace_id"]
    with pytest.raises(WorkspaceAffinityError, match="bound to workspace"):
        assert_workspace_affinity(project, second)


def test_affinity_rejects_same_workspace_with_another_state_dir(tmp_path: Path) -> None:
    first = tmp_path / "state-a"
    second = tmp_path / "state-b"
    project = {"id": "payments", "state_store_id": state_store_id(first)}

    assert assert_state_store_affinity(project, first) == project["state_store_id"]
    with pytest.raises(WorkspaceAffinityError, match="bound to state store"):
        assert_state_store_affinity(project, second)


def test_recovery_scanner_never_reads_runs_for_foreign_workspace() -> None:
    controller = object.__new__(ScheduledRunController)
    controller.registry = Mock()
    controller.registry.list_projects.return_value = [
        {"id": "local", "workspace_id": "workspace-a"},
        {"id": "foreign", "workspace_id": "workspace-b"},
    ]
    controller.registry.runs_for_project.return_value = [
        {"id": "run-local", "finished_at": None}
    ]
    controller._project_is_local = Mock(  # type: ignore[method-assign]
        side_effect=lambda project: project["id"] == "local"
    )

    assert list(controller._unfinished_records()) == [
        {"id": "run-local", "finished_at": None}
    ]
    controller.registry.runs_for_project.assert_called_once_with("local")
