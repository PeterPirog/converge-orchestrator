from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import Mock, patch

from converge_orchestrator.persistence import configured_control_db_path
from converge_orchestrator.runtime import RunController
from converge_orchestrator.runtime_service import ScheduledRunController


def test_run_controller_start_run_pins_config_and_uses_explicit_thread_id(
    tmp_path: Path,
) -> None:
    controller = object.__new__(RunController)
    controller.registry = Mock()
    project = {
        "id": "project",
        "config_path": str(tmp_path / "project.yaml"),
        "workspace_id": "workspace",
        "state_store_id": "state-store",
    }
    controller.registry.get_project.return_value = project
    controller.registry.runs_for_project.return_value = []
    controller.registry.create_run.return_value = {
        "id": "run-1",
        "project_id": "project",
        "thread_id": "cli-thread",
        "status": "queued",
    }
    controller._config_for_project = Mock(return_value=Mock())  # type: ignore[method-assign]
    controller._submit = Mock()  # type: ignore[method-assign]
    snapshot_path = tmp_path / "state" / "run-configs" / "run.yaml"
    snapshot_cfg = Mock(repo_path=tmp_path / "repo", state_dir=tmp_path / "state")

    with (
        patch(
            "converge_orchestrator.runtime.materialize_run_config_snapshot",
            return_value=(snapshot_cfg, snapshot_path, "config-sha"),
        ) as materialize,
        patch("converge_orchestrator.runtime.assert_workspace_affinity") as workspace_check,
        patch("converge_orchestrator.runtime.assert_state_store_affinity") as state_check,
    ):
        result = controller.start_run("project", thread_id="cli-thread")

    assert result["thread_id"] == "cli-thread"
    created_run_id = controller.registry.create_run.call_args.args[0]
    materialize.assert_called_once_with(project["config_path"], created_run_id)
    workspace_check.assert_called_once_with(project, snapshot_cfg.repo_path)
    state_check.assert_called_once_with(project, snapshot_cfg.state_dir)
    controller.registry.create_run.assert_called_once_with(
        created_run_id,
        "project",
        "cli-thread",
        config_snapshot_path=snapshot_path,
        config_snapshot_sha256="config-sha",
    )
    controller._submit.assert_called_once_with(
        created_run_id,
        {
            "project_id": "project",
            "config_path": str(snapshot_path),
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
    controller._config_for_run = Mock(return_value=Mock())  # type: ignore[method-assign]
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
