from __future__ import annotations

import subprocess
from pathlib import Path
from unittest.mock import patch

import pytest

from converge_orchestrator import restore_postgres
from converge_orchestrator.restore import RestorePlan, RestoreProjectPlan
from converge_orchestrator.restore_apply import RestoreApplyError, _ApplyJournal


def _plan(root: Path, binding: str) -> tuple[RestorePlan, _ApplyJournal]:
    project = RestoreProjectPlan(
        project_id="fixture",
        config_target=str(root / "project.yaml"),
        requirements_target=str(root / "architecture.md"),
        repo_target=str(root / "repository"),
        state_target=str(root / "state"),
        worktree_target=str(root / "state" / "worktrees"),
        repo_head="1" * 40,
        workspace_id="11111111-1111-1111-1111-111111111111",
        state_store_id="22222222-2222-2222-2222-222222222222",
    )
    payload = {
        "version": 1,
        "backup_manifest_sha256": "a" * 64,
        "persistence_backend": "postgres",
        "database_target": "postgres:configured",
        "database_target_binding": binding,
        "projects": [project.model_dump()],
        "blockers": [],
    }
    token = restore_postgres._stable_hash(payload)
    plan = RestorePlan(
        backup=str(root),
        backup_manifest_sha256="a" * 64,
        persistence_backend="postgres",
        database_target="postgres:configured",
        projects=[project],
        blockers=[],
        ready=True,
        confirmation_token=token,
    )
    journal = _ApplyJournal(
        confirmation_token=token,
        manifest_sha256="a" * 64,
        plan=plan.model_dump(mode="json"),
    )
    return plan, journal


def test_postgres_resume_plan_is_bound_to_exact_database_target(tmp_path: Path) -> None:
    binding = "b" * 64
    plan, journal = _plan(tmp_path, binding)

    restored = restore_postgres._resume_postgres_plan(
        journal,
        tmp_path,
        database_target_binding=binding,
    )

    assert restored == plan
    with pytest.raises(RestoreApplyError, match="confirmation token"):
        restore_postgres._resume_postgres_plan(
            journal,
            tmp_path,
            database_target_binding="c" * 64,
        )


def test_postgres_receipt_sql_contains_only_plan_bound_identity() -> None:
    sql = restore_postgres._receipt_sql(
        manifest_sha256="a" * 64,
        confirmation_token="b" * 64,
        database_target_binding="c" * 64,
    ).decode("ascii")

    assert "CREATE SCHEMA IF NOT EXISTS converge_restore_meta" in sql
    assert "CREATE TABLE IF NOT EXISTS converge_restore_meta.restore_receipt" in sql
    assert "DELETE FROM converge_restore_meta.restore_receipt" in sql
    assert "DROP SCHEMA" not in sql
    assert "a" * 64 in sql
    assert "b" * 64 in sql
    assert "c" * 64 in sql


@pytest.mark.parametrize("value", ["short", "g" * 64, "A" * 64])
def test_postgres_receipt_sql_rejects_noncanonical_identity(value: str) -> None:
    with pytest.raises(RestoreApplyError, match="receipt identity"):
        restore_postgres._receipt_sql(
            manifest_sha256=value,
            confirmation_token="b" * 64,
            database_target_binding="c" * 64,
        )


def test_psql_restore_keeps_database_url_out_of_argv(tmp_path: Path) -> None:
    script = tmp_path / "restore.sql"
    script.write_text("SELECT 1;\n", encoding="utf-8")
    completed = subprocess.CompletedProcess(args=[], returncode=0, stdout="")
    database_url = "postgresql://user:super-secret@database/converge"
    client_env = {
        "PGHOST": "database",
        "PGDATABASE": "converge",
        "PGUSER": "user",
        "PGPASSWORD": "super-secret",
    }

    with (
        patch(
            "converge_orchestrator.restore_postgres.libpq_env",
            return_value=client_env,
        ) as env_builder,
        patch(
            "converge_orchestrator.restore_postgres.subprocess.run",
            return_value=completed,
        ) as runner,
    ):
        restore_postgres._apply_restore_script(
            script=script,
            psql="/usr/bin/psql",
            database_url=database_url,
        )

    env_builder.assert_called_once()
    command = runner.call_args.args[0]
    assert database_url not in command
    assert "super-secret" not in " ".join(str(part) for part in command)
    assert "--single-transaction" in command
    assert "--set=ON_ERROR_STOP=1" in command
    assert runner.call_args.kwargs["env"]["PGDATABASE"] == "converge"
    assert runner.call_args.kwargs["env"]["PGPASSWORD"] == "super-secret"


def test_exact_server_receipt_is_adopted_without_second_restore(tmp_path: Path) -> None:
    _, journal = _plan(tmp_path, "b" * 64)
    journal_path = tmp_path / "journal.json"
    receipt = {
        "protocol_version": 1,
        "backup_manifest_sha256": "a" * 64,
        "confirmation_token": journal.confirmation_token,
        "database_target_binding": "b" * 64,
    }

    with (
        patch("converge_orchestrator.restore_postgres._read_receipt", return_value=receipt),
        patch("converge_orchestrator.restore_postgres._mark_published") as mark,
        patch("converge_orchestrator.restore_postgres._postgres_target_empty") as empty,
        patch("converge_orchestrator.restore_postgres._materialize_restore_script") as materialize,
    ):
        restore_postgres._ensure_postgres_database(
            root=tmp_path,
            database_url="postgresql://example.invalid/converge",
            pg_restore="pg_restore",
            psql="psql",
            manifest_sha256="a" * 64,
            database_target_binding="b" * 64,
            journal=journal,
            journal_path=journal_path,
        )

    mark.assert_called_once_with("database", journal, journal_path)
    empty.assert_not_called()
    materialize.assert_not_called()


def test_nonempty_target_without_receipt_fails_closed(tmp_path: Path) -> None:
    _, journal = _plan(tmp_path, "b" * 64)

    with (
        patch("converge_orchestrator.restore_postgres._read_receipt", return_value=None),
        patch("converge_orchestrator.restore_postgres._postgres_target_empty", return_value=False),
    ):
        with pytest.raises(RestoreApplyError, match="non-empty without the exact"):
            restore_postgres._ensure_postgres_database(
                root=tmp_path,
                database_url="postgresql://example.invalid/converge",
                pg_restore="pg_restore",
                psql="psql",
                manifest_sha256="a" * 64,
                database_target_binding="b" * 64,
                journal=journal,
                journal_path=tmp_path / "journal.json",
            )


def test_malformed_receipt_never_matches() -> None:
    assert not restore_postgres._receipt_matches(
        {
            "protocol_version": "invalid",
            "backup_manifest_sha256": "a" * 64,
            "confirmation_token": "b" * 64,
            "database_target_binding": "c" * 64,
        },
        manifest_sha256="a" * 64,
        confirmation_token="b" * 64,
        database_target_binding="c" * 64,
    )
