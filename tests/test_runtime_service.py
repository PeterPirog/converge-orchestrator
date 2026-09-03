from __future__ import annotations

import threading
from unittest.mock import Mock

import pytest

from converge_orchestrator.runtime_service import ScheduledRunController


def _controller() -> ScheduledRunController:
    controller = object.__new__(ScheduledRunController)
    controller.registry = Mock()
    controller._workers = {}
    controller._lock = threading.Lock()
    controller._lease_owner = "controller-test"
    controller._timers = {}
    controller._timer_generations = {}
    return controller


def _ci_interrupt() -> dict:
    return {
        "kind": "ci_wait",
        "run_id": "run-1",
        "head_sha": "abc",
        "wake_at": "2026-09-03T10:00:20+00:00",
        "poll_seconds": 20,
    }


def test_restore_ci_waits_rehydrates_timer_from_checkpoint() -> None:
    controller = _controller()
    controller.registry.list_projects.return_value = [{"id": "project"}]
    controller.registry.runs_for_project.return_value = [
        {"id": "run-1", "finished_at": None}
    ]
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
