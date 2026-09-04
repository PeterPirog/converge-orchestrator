from __future__ import annotations

from unittest.mock import Mock

from converge_orchestrator.affinity import project_affinity
from converge_orchestrator.workspace_identity import WorkspaceAffinityError


def _controller() -> Mock:
    controller = Mock()
    controller.registry.get_project.return_value = {"id": "payments"}
    controller.registry.runs_for_project.return_value = []
    return controller


def test_affinity_uses_current_project_config_only_when_no_run_is_active() -> None:
    controller = _controller()

    result = project_affinity(controller, "payments")

    assert result == {
        "project_id": "payments",
        "eligible": True,
        "basis": "project_config",
        "reason": "local",
        "unfinished_runs": 0,
    }
    controller._config_for_project.assert_called_once_with({"id": "payments"})
    controller._config_for_run.assert_not_called()


def test_affinity_uses_pinned_run_config_before_mutable_project_config() -> None:
    controller = _controller()
    active = {
        "id": "run-1",
        "project_id": "payments",
        "finished_at": None,
        "config_snapshot_path": "/state/run-configs/run-1.yaml",
        "config_snapshot_sha256": "sha",
    }
    controller.registry.runs_for_project.return_value = [active]

    result = project_affinity(controller, "payments")

    assert result["eligible"] is True
    assert result["basis"] == "pinned_run"
    controller._config_for_run.assert_called_once_with(active)
    controller._config_for_project.assert_not_called()


def test_affinity_mismatch_is_sanitized_for_scheduler_probe() -> None:
    controller = _controller()
    controller._config_for_project.side_effect = WorkspaceAffinityError(
        "contains worker-specific filesystem identifiers"
    )

    result = project_affinity(controller, "payments")

    assert result == {
        "project_id": "payments",
        "eligible": False,
        "basis": "project_config",
        "reason": "affinity_mismatch",
        "unfinished_runs": 0,
    }
    assert "worker-specific" not in str(result)


def test_affinity_fails_closed_for_ambiguous_unfinished_runs() -> None:
    controller = _controller()
    controller.registry.runs_for_project.return_value = [
        {"id": "run-1", "finished_at": None},
        {"id": "run-2", "finished_at": None},
    ]

    result = project_affinity(controller, "payments")

    assert result == {
        "project_id": "payments",
        "eligible": False,
        "basis": "ambiguous",
        "reason": "multiple_unfinished_runs",
        "unfinished_runs": 2,
    }
    controller._config_for_project.assert_not_called()
    controller._config_for_run.assert_not_called()


def test_affinity_classifies_missing_pinned_storage_without_fallback() -> None:
    controller = _controller()
    active = {"id": "run-1", "project_id": "payments", "finished_at": None}
    controller.registry.runs_for_project.return_value = [active]
    controller._config_for_run.side_effect = FileNotFoundError("snapshot missing")

    result = project_affinity(controller, "payments")

    assert result["eligible"] is False
    assert result["basis"] == "pinned_run"
    assert result["reason"] == "storage_unavailable"
    controller._config_for_project.assert_not_called()
