from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

from converge_orchestrator import backup_cli
from converge_orchestrator.restore_postgres import PostgresRestoreApplyResult


def test_restore_apply_dispatches_to_postgres_when_database_url_is_configured(
    tmp_path: Path,
) -> None:
    root = tmp_path / "backup"
    control_db = tmp_path / "unused-control.sqlite"
    database_url = "postgresql://user:secret@database/converge"
    token = "c" * 64
    result = PostgresRestoreApplyResult(projects=["payments"])

    with (
        patch.object(backup_cli, "configured_control_db_path", return_value=control_db),
        patch.object(backup_cli, "configured_database_url", return_value=database_url),
        patch.object(backup_cli, "apply_postgres_restore", return_value=result) as apply,
        patch.object(backup_cli, "apply_sqlite_restore") as sqlite_apply,
        patch.object(backup_cli.console, "print_json") as print_json,
    ):
        backup_cli.restore_apply(root, token)

    apply.assert_called_once_with(
        root,
        confirmation_token=token,
        control_db_path=control_db,
        database_url=database_url,
    )
    sqlite_apply.assert_not_called()
    print_json.assert_called_once_with(data=result.model_dump(mode="json"))
