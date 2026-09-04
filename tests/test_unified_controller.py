from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import Mock, patch

from converge_orchestrator.persistence import configured_control_db_path
from converge_orchestrator.runtime import RunController
from converge_orchestrator.runtime_service import ScheduledRunController


def test_run_controller_start_run_uses_explicit_thread_id() -> None:
    controller = object.__new__(RunController)
    controller.registry = Mock()
    controller.registry.runs_for_project.return_value = []
    controller.registry.create_run.return_value = {
        "id": "run-1",
        "project_id": "project",
        "thread_id": "cli-thread",
        "status": "queued",
    }
    controller._local_project = Mock(  # type: ignore[method-assign]
        return_value=({"config_path": "/tmp/project.yaml"}, Mock())
    )
    controller._submit = Mock()  # type: ignore[method-assign]

    result = controller.start_run("project", thread_id="cli-thread")

    assert result["thread_id"] == "cli-thread"
    created_run_id = controller.registry.create_run.call_args.args[0]
    controller.registry.create_run.assert_called_once_with(
        created_run_id,
        "project",
        "cli-thread",
    )
    controller._submit.assert_called_once_with(
        created_run_id,
        {
            "project_id": "project",
            "config_path": "/tmp/project.yaml",
            "run_id": created_run_id,
            "thread_id": "cli-thread",
        },
    )


def test_scheduled_controller_can_defer_broad_restore_for_cli_scope(tmp_path: Path) -> None:
    with (
        patch.object(RunController, "__init__", return_value=None),
        patch.object(ScheduledRunController, "restore_durable_runs") as restore,
    ):
        controller = ScheduledRunController(
            tmp_path / "control.sqlite",
            restore_on_start=False,
        )

    assert controller._timers == {}
    assert controller._timer_generations == {}
    restore.assert_not_called()


def test_status_reconciles_terminal_checkpoint_without_reexecution() -> None:
    controller = object.__new__(ScheduledRunController)
    controller.registry = Mock()
    controller.registry.get_run.return_value = {
        "id": "run-1",
        "status": "converged",
        "node": "done",
        "finished_at": "2026-09-04T03:00:00+00:00",
    }
    controller._cancel_timer = Mock()  # type: ignore[method-assign]
    snapshot = {
        "id": "run-1",
        "status": "converged",
        "finished_at": None,
        "values": {"status": "converged"},
        "next": [],
        "interrupt": None,
        "worker_alive": False,
        "remote_worker_active": False,
    }

    with patch.object(RunController, "status", return_value=snapshot):
        result = controller.status("run-1")

    controller.registry.update_run.assert_called_once_with(
        "run-1",
        status="converged",
        node="done",
        finished=True,
    )
    controller._cancel_timer.assert_called_once_with("run-1")
    assert result["finished_at"] == "2026-09-04T03:00:00+00:00"
    assert result["values"]["status"] == "converged"


def test_malformed_foreign_lease_fails_closed() -> None:
    controller = object.__new__(RunController)
    controller._lease_owner = "controller-local"

    assert controller._foreign_lease_active(
        {
            "lease_owner": "controller-other",
            "lease_expires_at": "not-a-timestamp",
        }
    )


def test_expired_foreign_lease_does_not_block_recovery() -> None:
    controller = object.__new__(RunController)
    controller._lease_owner = "controller-local"

    assert not controller._foreign_lease_active(
        {
            "lease_owner": "controller-other",
            "lease_expires_at": (datetime.now(UTC) - timedelta(seconds=1)).isoformat(),
        }
    )


def test_restart_recovery_never_inspects_run_with_live_foreign_lease() -> None:
    controller = object.__new__(ScheduledRunController)
    controller.registry = Mock()
    controller.registry.list_projects.return_value = [{"id": "project"}]
    controller.registry.runs_for_project.return_value = [
        {
            "id": "run-1",
            "project_id": "project",
            "thread_id": "thread-1",
            "status": "running",
            "finished_at": None,
            "lease_owner": "controller-other",
            "lease_expires_at": (
                datetime.now(UTC) + timedelta(minutes=5)
            ).isoformat(),
        }
    ]
    controller._lease_owner = "controller-local"
    controller._project_is_local = Mock(return_value=True)  # type: ignore[method-assign]
    controller._recovery_snapshot = Mock()  # type: ignore[method-assign]
    controller._schedule_recoverable = Mock()  # type: ignore[method-assign]

    controller._restore_recoverable_runs()

    controller._recovery_snapshot.assert_not_called()
    controller._schedule_recoverable.assert_not_called()
    controller.registry.update_run.assert_not_called()


def test_control_registry_path_is_shared_and_env_overridable(
    tmp_path: Path,
    monkeypatch,
) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("CONVERGE_CONTROL_DB", raising=False)

    assert configured_control_db_path() == (tmp_path / ".converge/control.sqlite").resolve()

    explicit = tmp_path / "shared" / "control.sqlite"
    monkeypatch.setenv("CONVERGE_CONTROL_DB", str(explicit))
    assert configured_control_db_path() == explicit.resolve()
