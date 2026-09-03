from __future__ import annotations

import threading
from unittest.mock import Mock

from converge_orchestrator.runtime_service import (
    ScheduledRunController,
    _is_initial_input_checkpoint,
)


def _record(*, status: str = "running") -> dict:
    return {
        "id": "run-1",
        "project_id": "project",
        "thread_id": "thread-1",
        "status": status,
        "finished_at": None,
    }


def _initial_values() -> dict:
    return {
        "project_id": "project",
        "config_path": "/tmp/project.yaml",
        "run_id": "run-1",
        "thread_id": "thread-1",
    }


def _controller() -> ScheduledRunController:
    controller = object.__new__(ScheduledRunController)
    controller.registry = Mock()
    controller._workers = {}
    controller._lock = threading.Lock()
    controller._lease_owner = "recovery-test"
    controller._timers = {}
    controller._timer_generations = {}
    return controller


def test_exact_initial_input_checkpoint_is_recoverable_but_extra_state_is_not() -> None:
    record = _record()
    snapshot = {"values": _initial_values(), "next": [], "interrupt": None}

    assert _is_initial_input_checkpoint(snapshot, record)

    snapshot["values"] = {**_initial_values(), "status": "running"}
    assert not _is_initial_input_checkpoint(snapshot, record)


def test_restore_schedules_start_for_durable_initial_input_checkpoint() -> None:
    controller = _controller()
    record = _record()
    controller._unfinished_records = Mock(return_value=iter([record]))  # type: ignore[method-assign]
    controller._recovery_snapshot = Mock(  # type: ignore[method-assign]
        return_value={"values": _initial_values(), "next": [], "interrupt": None}
    )
    controller._schedule_recoverable = Mock()  # type: ignore[method-assign]

    controller._restore_recoverable_runs()

    controller.registry.update_run.assert_called_once_with(
        "run-1", status="recoverable", node="start"
    )
    controller._schedule_recoverable.assert_called_once_with("run-1")


def test_resume_replays_original_input_for_durable_initial_input_checkpoint() -> None:
    controller = _controller()
    record = _record(status="recoverable")
    controller.registry.get_run.return_value = record
    controller._take_timer_generation = Mock(return_value=True)  # type: ignore[method-assign]
    controller._recovery_snapshot = Mock(  # type: ignore[method-assign]
        return_value={"values": _initial_values(), "next": [], "interrupt": None}
    )
    controller._initial_recovery_input = Mock(  # type: ignore[method-assign]
        return_value=_initial_values()
    )
    controller._submit = Mock()  # type: ignore[method-assign]

    controller._resume_recoverable("run-1", 7)

    controller._submit.assert_called_once_with("run-1", _initial_values())
