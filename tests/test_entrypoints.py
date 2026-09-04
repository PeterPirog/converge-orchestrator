from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import Mock, patch

import pytest
import typer

from converge_orchestrator import cli
from converge_orchestrator.runtime_service import ScheduledRunController


def test_cli_uses_scheduled_durable_controller() -> None:
    assert cli.ScheduledRunController is ScheduledRunController
    assert not hasattr(cli, "build_graph")


def test_cli_reuses_single_unfinished_run_instead_of_starting_another() -> None:
    controller = Mock()
    existing = {
        "id": "run-1",
        "thread_id": "thread-1",
        "finished_at": None,
    }
    controller.registry.runs_for_project.return_value = [existing]

    result = cli._select_or_start_run(controller, "payments", "thread-1")

    assert result == existing
    controller.start_run.assert_not_called()


def test_cli_rejects_ambiguous_multiple_unfinished_runs() -> None:
    controller = Mock()
    controller.registry.runs_for_project.return_value = [
        {"id": "run-1", "thread_id": "thread-1", "finished_at": None},
        {"id": "run-2", "thread_id": "thread-2", "finished_at": None},
    ]

    with pytest.raises(RuntimeError, match="multiple unfinished runs"):
        cli._select_or_start_run(controller, "payments", None)

    controller.start_run.assert_not_called()


def test_cli_rejects_reusing_thread_from_finished_run() -> None:
    controller = Mock()
    controller.registry.runs_for_project.return_value = [
        {
            "id": "run-old",
            "thread_id": "thread-stable",
            "finished_at": "2026-09-04T02:00:00+00:00",
        }
    ]

    with pytest.raises(RuntimeError, match="already belongs to finished run"):
        cli._select_or_start_run(controller, "payments", "thread-stable")

    controller.start_run.assert_not_called()


def test_cli_wait_does_not_surface_machine_ci_wait_as_hitl() -> None:
    controller = Mock()
    controller.registry.get_run.side_effect = [
        {"id": "run-1", "status": "interrupted", "finished_at": None},
        {
            "id": "run-1",
            "status": "converged",
            "finished_at": "2026-09-04T02:00:00+00:00",
        },
    ]
    controller.status.side_effect = [
        {"interrupt": {"kind": "ci_wait"}, "finished_at": None},
        {
            "interrupt": None,
            "finished_at": "2026-09-04T02:00:00+00:00",
            "values": {"status": "converged"},
        },
    ]

    with patch.object(cli.time, "sleep") as sleep:
        result = cli._wait_until_terminal_or_human_interrupt(
            controller,
            "run-1",
            poll_seconds=0.01,
        )

    assert result["values"]["status"] == "converged"
    sleep.assert_called_once_with(0.01)


def test_cli_wait_periodically_reconciles_recoverable_state() -> None:
    controller = Mock()
    controller.registry.get_run.side_effect = [
        {"id": "run-1", "status": "recoverable", "finished_at": None},
        {
            "id": "run-1",
            "status": "converged",
            "finished_at": "2026-09-04T02:00:00+00:00",
        },
    ]
    controller.status.side_effect = [
        {
            "id": "run-1",
            "status": "recoverable",
            "finished_at": None,
            "interrupt": None,
        },
        {
            "id": "run-1",
            "status": "converged",
            "finished_at": "2026-09-04T02:00:00+00:00",
            "values": {"status": "converged"},
        },
    ]

    with (
        patch.object(cli.time, "monotonic", side_effect=[0.0, 6.0, 6.0]),
        patch.object(cli.time, "sleep") as sleep,
    ):
        result = cli._wait_until_terminal_or_human_interrupt(
            controller,
            "run-1",
            poll_seconds=0.01,
            reconcile_seconds=5.0,
        )

    assert result["values"]["status"] == "converged"
    assert controller.status.call_count == 2
    sleep.assert_called_once_with(0.01)


def test_cli_run_registers_and_starts_through_scheduled_controller(tmp_path) -> None:
    config = tmp_path / "converge.yaml"
    cfg = SimpleNamespace(project_name="payments")
    controller = Mock()
    controller.registry.list_projects.return_value = []
    controller.registry.runs_for_project.return_value = []
    controller.start_run.return_value = {"id": "run-1", "thread_id": "thread-1"}
    terminal = {
        "id": "run-1",
        "finished_at": "2026-09-04T02:00:00+00:00",
        "values": {"status": "converged"},
    }

    with (
        patch.object(cli, "load_config", return_value=cfg),
        patch.object(
            cli,
            "configured_control_db_path",
            return_value=tmp_path / "control.sqlite",
        ),
        patch.object(cli, "ScheduledRunController", return_value=controller) as factory,
        patch.object(
            cli,
            "_wait_until_terminal_or_human_interrupt",
            return_value=terminal,
        ) as wait,
        patch.object(cli.console, "print_json") as print_json,
    ):
        cli.run(config, thread_id=None, project_id=None)

    factory.assert_called_once_with(
        tmp_path / "control.sqlite",
        restore_on_start=False,
    )
    controller.register_project.assert_called_once_with("payments", config.resolve())
    controller.restore_durable_runs.assert_called_once_with("payments")
    controller.start_run.assert_called_once_with("payments", thread_id=None)
    wait.assert_called_once_with(controller, "run-1")
    print_json.assert_called_once_with(data=terminal)


def test_cli_recovery_does_not_load_or_reregister_changed_source_config(tmp_path) -> None:
    config = tmp_path / "converge.yaml"
    config.write_text("this: is: no longer valid yaml\n", encoding="utf-8")
    controller = Mock()
    controller.registry.list_projects.return_value = [
        {
            "id": "payments",
            "config_path": str(config.resolve()),
        }
    ]
    existing = {
        "id": "run-1",
        "project_id": "payments",
        "thread_id": "thread-1",
        "finished_at": None,
    }
    controller.registry.runs_for_project.return_value = [existing]
    terminal = {
        **existing,
        "status": "converged",
        "finished_at": "2026-09-04T03:00:00+00:00",
    }

    with (
        patch.object(
            cli,
            "configured_control_db_path",
            return_value=tmp_path / "control.sqlite",
        ),
        patch.object(cli, "ScheduledRunController", return_value=controller),
        patch.object(cli, "load_config") as load_config,
        patch.object(
            cli,
            "_wait_until_terminal_or_human_interrupt",
            return_value=terminal,
        ) as wait,
        patch.object(cli.console, "print_json"),
    ):
        cli.run(config, thread_id=None, project_id=None)

    load_config.assert_not_called()
    controller.register_project.assert_not_called()
    controller.restore_durable_runs.assert_called_once_with("payments")
    controller.start_run.assert_not_called()
    wait.assert_called_once_with(controller, "run-1")


def test_cli_recovery_rejects_explicit_project_config_path_mismatch(tmp_path) -> None:
    config = tmp_path / "other.yaml"
    controller = Mock()
    controller.registry.get_project.return_value = {
        "id": "payments",
        "config_path": str((tmp_path / "payments.yaml").resolve()),
    }

    with pytest.raises(typer.BadParameter, match="registered with config"):
        with (
            patch.object(
                cli,
                "configured_control_db_path",
                return_value=tmp_path / "control.sqlite",
            ),
            patch.object(cli, "ScheduledRunController", return_value=controller),
        ):
            cli.run(config, thread_id=None, project_id="payments")


def test_cli_project_id_must_be_api_compatible(tmp_path) -> None:
    cfg = SimpleNamespace(project_name="not a valid id")

    with pytest.raises(typer.BadParameter, match="project ID must match"):
        cli._resolve_cli_project_id(tmp_path / "converge.yaml", cfg, None)
