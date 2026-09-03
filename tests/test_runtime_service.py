from __future__ import annotations

import sqlite3
import threading
from pathlib import Path
from unittest.mock import Mock, patch

import pytest

from converge_orchestrator.models import ProjectConfig
from converge_orchestrator.remote import RemoteValidationError
from converge_orchestrator.runtime_service import ScheduledRunController


def _controller() -> ScheduledRunController:
    controller = object.__new__(ScheduledRunController)
    controller.registry = Mock()
    controller.persistence = Mock()
    controller.persistence.is_transient_error.side_effect = lambda exc: (
        isinstance(exc, sqlite3.OperationalError)
        and any(marker in str(exc).lower() for marker in ("locked", "busy"))
    )
    controller._workers = {}
    controller._lock = threading.Lock()
    controller._lease_owner = "controller-test"
    controller._timers = {}
    controller._timer_generations = {}
    controller._project_is_local = Mock(return_value=True)  # type: ignore[method-assign]
    return controller


def _ci_interrupt() -> dict:
    return {
        "kind": "ci_wait",
        "run_id": "run-1",
        "head_sha": "abc",
        "wake_at": "2026-09-03T10:00:20+00:00",
        "poll_seconds": 20,
    }


def _unfinished_record() -> dict:
    return {
        "id": "run-1",
        "project_id": "project",
        "thread_id": "thread-1",
        "status": "running",
        "finished_at": None,
    }


def _one_unfinished_run(controller: ScheduledRunController) -> None:
    controller.registry.list_projects.return_value = [{"id": "project"}]
    controller.registry.runs_for_project.return_value = [_unfinished_record()]


def test_restore_ci_waits_rehydrates_timer_from_checkpoint() -> None:
    controller = _controller()
    _one_unfinished_run(controller)
    controller._snapshot = Mock(  # type: ignore[method-assign]
        return_value={"interrupt": _ci_interrupt()}
    )
    controller._schedule_ci_wait = Mock()  # type: ignore[method-assign]

    controller._restore_ci_waits()

    controller.registry.update_run.assert_called_once_with(
        "run-1", status="waiting_ci", node="ci_wait"
    )
    controller._schedule_ci_wait.assert_called_once_with(
        "run-1", "2026-09-03T10:00:20+00:00"
    )


def test_restore_recoverable_run_schedules_automatic_langgraph_resume() -> None:
    controller = _controller()
    _one_unfinished_run(controller)
    controller._recovery_snapshot = Mock(  # type: ignore[method-assign]
        return_value={"values": {"status": "built"}, "interrupt": None, "next": ["quality"]}
    )
    controller._schedule_recoverable = Mock()  # type: ignore[method-assign]

    controller._restore_recoverable_runs()

    controller.registry.update_run.assert_called_once_with(
        "run-1", status="recoverable", node="quality"
    )
    controller._schedule_recoverable.assert_called_once_with("run-1")


def test_restore_precheckpoint_run_schedules_initial_recovery() -> None:
    controller = _controller()
    _one_unfinished_run(controller)
    controller._recovery_snapshot = Mock(  # type: ignore[method-assign]
        return_value={"values": {}, "interrupt": None, "next": []}
    )
    controller._schedule_recoverable = Mock()  # type: ignore[method-assign]

    controller._restore_recoverable_runs()

    controller.registry.update_run.assert_called_once_with(
        "run-1", status="recoverable", node="start"
    )
    controller._schedule_recoverable.assert_called_once_with("run-1")


def test_restore_terminal_checkpoint_finishes_stale_registry_record() -> None:
    controller = _controller()
    _one_unfinished_run(controller)
    controller._recovery_snapshot = Mock(  # type: ignore[method-assign]
        return_value={"values": {"status": "completed"}, "interrupt": None, "next": []}
    )
    controller._schedule_recoverable = Mock()  # type: ignore[method-assign]
    controller._cancel_timer = Mock()  # type: ignore[method-assign]

    controller._restore_recoverable_runs()

    controller.registry.update_run.assert_called_once_with(
        "run-1", status="completed", node="done", finished=True
    )
    controller._cancel_timer.assert_called_once_with("run-1")
    controller._schedule_recoverable.assert_not_called()


def test_restore_does_not_resume_human_or_controlled_interrupt() -> None:
    controller = _controller()
    _one_unfinished_run(controller)
    controller._recovery_snapshot = Mock(  # type: ignore[method-assign]
        return_value={
            "values": {"status": "interrupted"},
            "interrupt": {"kind": "risk_policy"},
            "next": ["human"],
        }
    )
    controller._schedule_recoverable = Mock()  # type: ignore[method-assign]

    controller._restore_recoverable_runs()

    controller._schedule_recoverable.assert_not_called()
    controller.registry.update_run.assert_not_called()


def test_automatic_recovery_resumes_existing_thread_without_new_input() -> None:
    controller = _controller()
    record = _unfinished_record()
    controller.registry.get_run.return_value = record
    controller._timer_generations["run-1"] = 3
    controller._recovery_snapshot = Mock(  # type: ignore[method-assign]
        return_value={"values": {"status": "built"}, "interrupt": None, "next": ["quality"]}
    )
    controller._submit = Mock()  # type: ignore[method-assign]

    controller._resume_recoverable("run-1", 3)

    controller._submit.assert_called_once_with("run-1", None)


def test_automatic_precheckpoint_recovery_replays_original_initial_input() -> None:
    controller = _controller()
    record = {**_unfinished_record(), "status": "recoverable"}
    controller.registry.get_run.return_value = record
    controller.registry.get_project.return_value = {"config_path": "/tmp/project.yaml"}
    controller._timer_generations["run-1"] = 5
    controller._recovery_snapshot = Mock(  # type: ignore[method-assign]
        return_value={"values": {}, "interrupt": None, "next": []}
    )
    controller._submit = Mock()  # type: ignore[method-assign]

    controller._resume_recoverable("run-1", 5)

    controller._submit.assert_called_once_with(
        "run-1",
        {
            "project_id": "project",
            "config_path": "/tmp/project.yaml",
            "run_id": "run-1",
            "thread_id": "thread-1",
        },
    )


def test_recovery_timer_reconciles_terminal_checkpoint_without_reexecution() -> None:
    controller = _controller()
    record = {**_unfinished_record(), "status": "recoverable"}
    controller.registry.get_run.return_value = record
    controller._timer_generations["run-1"] = 6
    controller._recovery_snapshot = Mock(  # type: ignore[method-assign]
        return_value={"values": {"status": "converged"}, "interrupt": None, "next": []}
    )
    controller._submit = Mock()  # type: ignore[method-assign]

    controller._resume_recoverable("run-1", 6)

    controller.registry.update_run.assert_called_once_with(
        "run-1", status="converged", node="done", finished=True
    )
    controller._submit.assert_not_called()


def test_recovery_inspection_failure_never_restarts_from_empty_state() -> None:
    controller = _controller()
    record = _unfinished_record()
    controller._open_graph = Mock(  # type: ignore[method-assign]
        side_effect=sqlite3.DatabaseError("corrupt checkpoint")
    )
    controller._schedule_recoverable = Mock()  # type: ignore[method-assign]

    snapshot = controller._recovery_snapshot(record)

    assert snapshot is None
    controller.registry.update_run.assert_called_once()
    update = controller.registry.update_run.call_args
    assert update.args[0] == "run-1"
    assert "checkpoint inspection failed" in update.kwargs["error"]
    controller._schedule_recoverable.assert_not_called()


def test_transient_checkpoint_lock_schedules_automatic_inspection_retry() -> None:
    controller = _controller()
    record = _unfinished_record()
    controller._open_graph = Mock(  # type: ignore[method-assign]
        side_effect=sqlite3.OperationalError("database is locked")
    )
    controller._schedule_recoverable = Mock()  # type: ignore[method-assign]

    snapshot = controller._recovery_snapshot(record)

    assert snapshot is None
    controller.registry.update_run.assert_called_once()
    update = controller.registry.update_run.call_args
    assert update.args[0] == "run-1"
    assert "database is locked" in update.kwargs["error"]
    controller._schedule_recoverable.assert_called_once_with("run-1", 1.0)


def test_automatic_recovery_retries_when_another_controller_holds_lease() -> None:
    controller = _controller()
    controller.registry.get_run.return_value = _unfinished_record()
    controller._timer_generations["run-1"] = 4
    controller._recovery_snapshot = Mock(  # type: ignore[method-assign]
        return_value={
            "values": {"status": "pushed"},
            "interrupt": None,
            "next": ["integrate"],
        }
    )
    controller._submit = Mock(  # type: ignore[method-assign]
        side_effect=RuntimeError("Run is leased by another active controller")
    )
    controller._schedule_recoverable = Mock()  # type: ignore[method-assign]

    controller._resume_recoverable("run-1", 4)

    controller._schedule_recoverable.assert_called_once_with("run-1", 5.0)
    controller.registry.update_run.assert_not_called()


def test_machine_ci_wait_cannot_be_decided_as_hitl() -> None:
    controller = _controller()
    controller.registry.get_run.return_value = {"id": "run-1"}
    controller._snapshot = Mock(  # type: ignore[method-assign]
        return_value={"interrupt": _ci_interrupt()}
    )

    with pytest.raises(RuntimeError, match="machine-managed"):
        controller.decide("run-1", {"action": "approve"})


def test_manual_resume_can_trigger_early_ci_poll() -> None:
    controller = _controller()
    record = {"id": "run-1"}
    controller.registry.get_run.return_value = record
    controller._snapshot = Mock(  # type: ignore[method-assign]
        return_value={"interrupt": _ci_interrupt()}
    )
    controller._submit = Mock()  # type: ignore[method-assign]
    controller._cancel_timer = Mock()  # type: ignore[method-assign]

    result = controller.resume("run-1")

    assert result == record
    controller._submit.assert_called_once()
    assert controller._submit.call_args.args[0] == "run-1"
    assert controller._submit.call_args.args[1].resume == "resume"
    controller._cancel_timer.assert_called_once_with("run-1")


def test_waiting_ci_counts_as_active_project_run() -> None:
    controller = _controller()
    controller.registry.runs_for_project.return_value = [
        {"id": "run-1", "status": "waiting_ci", "finished_at": None}
    ]

    with pytest.raises(RuntimeError, match="already has active run"):
        controller.start_run("project")


def test_registration_rejects_github_origin_mismatch(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    (repo / ".git").mkdir(parents=True)
    requirements = tmp_path / "architecture.md"
    requirements.write_text("Architecture\n", encoding="utf-8")
    cfg = ProjectConfig(
        repo_path=repo,
        requirements_path=requirements,
        github_repo="owner/repo",
        agents={},
    )
    controller = _controller()

    with (
        patch("converge_orchestrator.runtime_service.load_config", return_value=cfg),
        patch(
            "converge_orchestrator.runtime_service.validate_origin_repository",
            side_effect=RemoteValidationError(
                "Git origin mismatch: expected owner/repo, found owner/other"
            ),
        ),
    ):
        with pytest.raises(ValueError, match="Git origin mismatch"):
            controller.register_project("project", tmp_path / "project.yaml")

    controller.registry.register_project.assert_not_called()
