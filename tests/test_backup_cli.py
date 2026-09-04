from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

import pytest
import typer

from converge_orchestrator import backup_cli
from converge_orchestrator.backup import BackupError, BackupManifest
from converge_orchestrator.restore import RestorePlan


def _manifest(backend: str = "sqlite") -> BackupManifest:
    return BackupManifest(
        created_at="2026-09-04T06:30:00+00:00",
        persistence_backend=backend,
        projects=[],
        files=[],
    )


def _restore_plan(root: Path, *, ready: bool) -> RestorePlan:
    blockers = [] if ready else ["repository restore target already exists"]
    return RestorePlan(
        backup=str(root.resolve()),
        backup_manifest_sha256="a" * 64,
        persistence_backend="sqlite",
        database_target=str((root.parent / "control.sqlite").resolve()),
        projects=[],
        blockers=blockers,
        ready=ready,
        confirmation_token="b" * 64,
    )


def test_create_backup_uses_selected_durable_persistence_backend(tmp_path: Path) -> None:
    control_db = tmp_path / "control.sqlite"
    destination = tmp_path / "backup"
    registry = Mock()
    backend = SimpleNamespace(
        registry=registry,
        kind="sqlite",
        control_db_path=control_db,
        database_url=None,
    )
    manifest = _manifest()

    with (
        patch.object(backup_cli, "configured_control_db_path", return_value=control_db),
        patch.object(backup_cli, "PersistenceBackend", return_value=backend) as factory,
        patch.object(
            backup_cli,
            "create_deployment_backup",
            return_value=manifest,
        ) as create,
        patch.object(backup_cli.console, "print_json") as print_json,
    ):
        backup_cli.create_backup(destination)

    factory.assert_called_once_with(control_db)
    create.assert_called_once_with(
        registry=registry,
        persistence_backend="sqlite",
        control_db_path=control_db,
        database_url=None,
        destination=destination,
    )
    print_json.assert_called_once_with(
        data={
            "status": "created",
            "backup": str(destination.resolve()),
            "created_at": manifest.created_at,
            "persistence_backend": "sqlite",
            "projects": 0,
            "files": 0,
        }
    )


def test_verify_backup_is_offline_and_does_not_open_persistence(tmp_path: Path) -> None:
    root = tmp_path / "backup"
    manifest = _manifest("postgres")

    with (
        patch.object(backup_cli, "PersistenceBackend") as backend,
        patch.object(
            backup_cli,
            "verify_deployment_backup",
            return_value=manifest,
        ) as verify,
        patch.object(backup_cli.console, "print_json") as print_json,
    ):
        backup_cli.verify_backup(root)

    backend.assert_not_called()
    verify.assert_called_once_with(root)
    print_json.assert_called_once_with(
        data={
            "status": "verified",
            "backup": str(root.resolve()),
            "created_at": manifest.created_at,
            "persistence_backend": "postgres",
            "projects": 0,
            "files": 0,
        }
    )


def test_backup_cli_converts_backup_failure_to_operator_error(tmp_path: Path) -> None:
    backend = SimpleNamespace(
        registry=Mock(),
        kind="sqlite",
        control_db_path=tmp_path / "control.sqlite",
        database_url=None,
    )
    with (
        patch.object(backup_cli, "PersistenceBackend", return_value=backend),
        patch.object(
            backup_cli,
            "create_deployment_backup",
            side_effect=BackupError("deployment is not quiescent"),
        ),
        pytest.raises(typer.BadParameter, match="deployment is not quiescent"),
    ):
        backup_cli.create_backup(tmp_path / "backup")


def test_restore_plan_uses_environment_targets_without_opening_runtime(tmp_path: Path) -> None:
    root = tmp_path / "backup"
    control_db = tmp_path / "restored-control.sqlite"
    plan = _restore_plan(root, ready=True)

    with (
        patch.object(backup_cli, "PersistenceBackend") as backend,
        patch.object(backup_cli, "configured_control_db_path", return_value=control_db),
        patch.object(backup_cli, "configured_database_url", return_value=None),
        patch.object(
            backup_cli,
            "plan_deployment_restore",
            return_value=plan,
        ) as planner,
        patch.object(backup_cli.console, "print_json") as print_json,
    ):
        backup_cli.restore_plan(root)

    backend.assert_not_called()
    planner.assert_called_once_with(
        root,
        control_db_path=control_db,
        database_url=None,
    )
    print_json.assert_called_once_with(data=plan.model_dump(mode="json"))


def test_blocked_restore_plan_returns_nonzero_after_printing_evidence(tmp_path: Path) -> None:
    root = tmp_path / "backup"
    plan = _restore_plan(root, ready=False)

    with (
        patch.object(backup_cli, "configured_control_db_path", return_value=tmp_path / "control"),
        patch.object(backup_cli, "configured_database_url", return_value=None),
        patch.object(backup_cli, "plan_deployment_restore", return_value=plan),
        patch.object(backup_cli.console, "print_json") as print_json,
        pytest.raises(typer.Exit) as raised,
    ):
        backup_cli.restore_plan(root)

    assert raised.value.exit_code == 2
    print_json.assert_called_once_with(data=plan.model_dump(mode="json"))
