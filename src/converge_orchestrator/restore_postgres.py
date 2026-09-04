from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel

from .backup import BackupManifest, verify_deployment_backup
from .restore import (
    RestoreError,
    RestorePlan,
    _postgres_target_binding,
    _postgres_target_empty,
    plan_deployment_restore,
)
from .restore_apply import (
    RestoreApplyError,
    _ApplyJournal,
    _create_journal,
    _ensure_empty_worktree,
    _ensure_file,
    _ensure_repo,
    _ensure_state,
    _expected_journal_keys,
    _journal_path,
    _link_like,
    _load_journal,
    _manifest_project,
    _mark_published,
    _new_journal,
    _remove_stage,
    _sha256,
    _stable_hash,
    _validate_journal_keys,
    _write_journal,
)

_POSTGRES_RESTORE_TIMEOUT_SECONDS = 1800
_RECEIPT_SCHEMA = "converge_restore_meta"
_RECEIPT_TABLE = "restore_receipt"


class PostgresRestoreApplyResult(BaseModel):
    status: Literal["restored"] = "restored"
    persistence_backend: Literal["postgres"] = "postgres"
    projects: list[str]
    resumed: bool = False


def _postgres_modules():
    try:
        import psycopg
        from psycopg.rows import dict_row
    except ImportError as exc:  # pragma: no cover - optional dependency guard
        raise RestoreApplyError(
            "PostgreSQL restore requires `pip install 'converge-orchestrator[postgres]'`"
        ) from exc
    return psycopg, dict_row


def _validate_confirmation_token(value: str) -> None:
    if len(value) != 64 or any(char not in "0123456789abcdef" for char in value.lower()):
        raise RestoreApplyError("confirmation token must be a 64-character SHA-256 value")


def _tool(name: str) -> str:
    executable = shutil.which(name)
    if executable is None:
        raise RestoreApplyError(f"PostgreSQL restore requires {name} on PATH")
    return executable


def _safe_detail(raw: str, database_url: str | None = None) -> str:
    detail = raw.strip()[-2000:]
    if database_url:
        detail = detail.replace(database_url, "<redacted>")
    return detail


def _validate_postgres_archive(archive: Path) -> tuple[str, str]:
    pg_restore = _tool("pg_restore")
    psql = _tool("psql")
    try:
        result = subprocess.run(
            [pg_restore, "--list", str(archive)],
            text=True,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            timeout=300,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise RestoreApplyError("PostgreSQL backup archive could not be inspected") from exc
    if result.returncode != 0:
        raise RestoreApplyError(
            "PostgreSQL backup archive is not readable by pg_restore: "
            f"{_safe_detail(result.stderr)}"
        )
    return pg_restore, psql


def _receipt_sql(
    *,
    manifest_sha256: str,
    confirmation_token: str,
    database_target_binding: str,
) -> bytes:
    values = (manifest_sha256, confirmation_token, database_target_binding)
    invalid = any(
        len(value) != 64
        or any(char not in "0123456789abcdef" for char in value)
        for value in values
    )
    if invalid:
        raise RestoreApplyError("PostgreSQL restore receipt identity is invalid")
    sql = f"""

CREATE SCHEMA IF NOT EXISTS {_RECEIPT_SCHEMA};
CREATE TABLE IF NOT EXISTS {_RECEIPT_SCHEMA}.{_RECEIPT_TABLE} (
    singleton smallint PRIMARY KEY CHECK (singleton = 1),
    protocol_version integer NOT NULL,
    backup_manifest_sha256 char(64) NOT NULL,
    confirmation_token char(64) NOT NULL,
    database_target_binding char(64) NOT NULL
);
DELETE FROM {_RECEIPT_SCHEMA}.{_RECEIPT_TABLE};
INSERT INTO {_RECEIPT_SCHEMA}.{_RECEIPT_TABLE}(
    singleton,
    protocol_version,
    backup_manifest_sha256,
    confirmation_token,
    database_target_binding
) VALUES (
    1,
    1,
    '{manifest_sha256}',
    '{confirmation_token}',
    '{database_target_binding}'
);
"""
    return sql.encode("ascii")


def _postgres_stage_dir(root: Path, confirmation_token: str) -> Path:
    return root.parent / (
        f".{root.name}.converge-restore-{confirmation_token[:16]}.postgres-stage"
    )


def _materialize_restore_script(
    *,
    root: Path,
    confirmation_token: str,
    pg_restore: str,
    manifest_sha256: str,
    database_target_binding: str,
) -> Path:
    stage = _postgres_stage_dir(root, confirmation_token)
    _remove_stage(stage)
    stage.mkdir(mode=0o700)
    script = stage / "restore.sql"
    archive = root / "database" / "postgres.dump"
    try:
        result = subprocess.run(
            [pg_restore, "--exit-on-error", "--file", str(script), str(archive)],
            text=True,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            timeout=_POSTGRES_RESTORE_TIMEOUT_SECONDS,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        _remove_stage(stage)
        raise RestoreApplyError("pg_restore could not materialize the restore script") from exc
    if result.returncode != 0:
        detail = _safe_detail(result.stderr)
        _remove_stage(stage)
        raise RestoreApplyError(
            f"pg_restore failed with exit code {result.returncode}: {detail}"
        )
    if _link_like(script) or not script.is_file():
        _remove_stage(stage)
        raise RestoreApplyError("pg_restore did not produce a safe restore script")
    try:
        with script.open("ab") as handle:
            handle.write(
                _receipt_sql(
                    manifest_sha256=manifest_sha256,
                    confirmation_token=confirmation_token,
                    database_target_binding=database_target_binding,
                )
            )
            handle.flush()
            os.fsync(handle.fileno())
    except OSError as exc:
        _remove_stage(stage)
        raise RestoreApplyError("PostgreSQL restore receipt could not be staged") from exc
    return script


def _apply_restore_script(
    *,
    script: Path,
    psql: str,
    database_url: str,
) -> None:
    env = os.environ.copy()
    env["PGDATABASE"] = database_url
    try:
        result = subprocess.run(
            [
                psql,
                "--no-psqlrc",
                "--single-transaction",
                "--set=ON_ERROR_STOP=1",
                "--file",
                str(script),
            ],
            env=env,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            timeout=_POSTGRES_RESTORE_TIMEOUT_SECONDS,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise RestoreApplyError("PostgreSQL restore transaction could not be executed") from exc
    if result.returncode != 0:
        raise RestoreApplyError(
            f"PostgreSQL restore transaction failed with exit code {result.returncode}: "
            f"{_safe_detail(result.stdout, database_url)}"
        )


def _read_receipt(database_url: str) -> dict[str, Any] | None:
    psycopg, dict_row = _postgres_modules()
    relation_name = f"{_RECEIPT_SCHEMA}.{_RECEIPT_TABLE}"
    try:
        with psycopg.connect(database_url, autocommit=True, row_factory=dict_row) as db:
            relation = db.execute(
                "SELECT to_regclass(%s) AS relation",
                (relation_name,),
            ).fetchone()
            if relation is None or relation["relation"] is None:
                return None
            rows = db.execute(
                f"""
                SELECT protocol_version,
                       backup_manifest_sha256,
                       confirmation_token,
                       database_target_binding
                FROM {_RECEIPT_SCHEMA}.{_RECEIPT_TABLE}
                WHERE singleton = 1
                """
            ).fetchall()
    except Exception as exc:
        raise RestoreApplyError("PostgreSQL restore receipt could not be inspected") from exc
    if len(rows) != 1:
        raise RestoreApplyError("PostgreSQL restore receipt is ambiguous")
    return dict(rows[0])


def _receipt_matches(
    receipt: dict[str, Any] | None,
    *,
    manifest_sha256: str,
    confirmation_token: str,
    database_target_binding: str,
) -> bool:
    if receipt is None:
        return False
    try:
        protocol_version = int(receipt.get("protocol_version") or 0)
    except (TypeError, ValueError):
        return False
    return (
        protocol_version == 1
        and str(receipt.get("backup_manifest_sha256") or "").strip() == manifest_sha256
        and str(receipt.get("confirmation_token") or "").strip() == confirmation_token
        and str(receipt.get("database_target_binding") or "").strip()
        == database_target_binding
    )


def _resume_postgres_plan(
    journal: _ApplyJournal,
    root: Path,
    *,
    database_target_binding: str,
) -> RestorePlan:
    try:
        plan = RestorePlan.model_validate(journal.plan)
    except ValueError as exc:
        raise RestoreApplyError("restore journal contains an invalid plan") from exc
    if not plan.ready or plan.blockers:
        raise RestoreApplyError("restore journal did not originate from a ready preflight")
    if plan.backup != str(root):
        raise RestoreApplyError("restore journal backup path mismatch")
    if plan.persistence_backend != "postgres" or plan.database_target != "postgres:configured":
        raise RestoreApplyError("restore journal backend mismatch")
    token_payload = {
        "version": plan.version,
        "backup_manifest_sha256": plan.backup_manifest_sha256,
        "persistence_backend": plan.persistence_backend,
        "database_target": plan.database_target,
        "database_target_binding": database_target_binding,
        "projects": [project.model_dump() for project in plan.projects],
        "blockers": plan.blockers,
    }
    if _stable_hash(token_payload) != journal.confirmation_token:
        raise RestoreApplyError("restore journal plan no longer matches its confirmation token")
    return plan


def _ensure_postgres_database(
    *,
    root: Path,
    database_url: str,
    pg_restore: str,
    psql: str,
    manifest_sha256: str,
    database_target_binding: str,
    journal: _ApplyJournal,
    journal_path: Path,
) -> None:
    receipt = _read_receipt(database_url)
    exact = _receipt_matches(
        receipt,
        manifest_sha256=manifest_sha256,
        confirmation_token=journal.confirmation_token,
        database_target_binding=database_target_binding,
    )
    if "database" in journal.published:
        if not exact:
            raise RestoreApplyError("published PostgreSQL restore receipt changed or disappeared")
        return
    if receipt is not None:
        if not exact:
            raise RestoreApplyError("PostgreSQL target contains a different restore receipt")
        _mark_published("database", journal, journal_path)
        return

    try:
        empty = _postgres_target_empty(database_url)
    except RestoreError as exc:
        raise RestoreApplyError(str(exc)) from exc
    if not empty:
        raise RestoreApplyError(
            "PostgreSQL target is non-empty without the exact committed restore receipt"
        )

    script = _materialize_restore_script(
        root=root,
        confirmation_token=journal.confirmation_token,
        pg_restore=pg_restore,
        manifest_sha256=manifest_sha256,
        database_target_binding=database_target_binding,
    )
    stage = script.parent
    try:
        _apply_restore_script(script=script, psql=psql, database_url=database_url)
    finally:
        _remove_stage(stage)

    receipt = _read_receipt(database_url)
    if not _receipt_matches(
        receipt,
        manifest_sha256=manifest_sha256,
        confirmation_token=journal.confirmation_token,
        database_target_binding=database_target_binding,
    ):
        raise RestoreApplyError("PostgreSQL restore committed without the expected receipt")
    _mark_published("database", journal, journal_path)


def _restore_filesystem(
    *,
    root: Path,
    manifest: BackupManifest,
    plan: RestorePlan,
    journal: _ApplyJournal,
    journal_path: Path,
) -> None:
    database_sentinel = root / ".postgres-database-target"
    for project_plan in plan.projects:
        project = _manifest_project(manifest, project_plan.project_id)
        project_root = root / "projects" / project.project_id
        config_target = Path(project_plan.config_target)
        requirements_target = Path(project_plan.requirements_target)
        repo_target = Path(project_plan.repo_target)
        state_target = Path(project_plan.state_target)
        worktree_target = Path(project_plan.worktree_target)

        _ensure_file(
            key=f"{project.project_id}:requirements",
            source=project_root / "requirements.md",
            target=requirements_target,
            expected_sha256=project.requirements_sha256,
            journal=journal,
            journal_path=journal_path,
        )
        _ensure_file(
            key=f"{project.project_id}:config",
            source=project_root / "converge.yaml",
            target=config_target,
            expected_sha256=project.config_sha256,
            journal=journal,
            journal_path=journal_path,
        )
        _ensure_repo(root, project, repo_target, journal, journal_path)
        _ensure_state(
            root,
            manifest,
            project,
            state_target,
            worktree_target,
            database_sentinel,
            journal,
            journal_path,
        )
        _ensure_empty_worktree(
            project=project,
            target=worktree_target,
            state_target=state_target,
            journal=journal,
            journal_path=journal_path,
        )


def apply_postgres_restore(
    backup_root: Path,
    *,
    confirmation_token: str,
    control_db_path: Path,
    database_url: str | None,
) -> PostgresRestoreApplyResult:
    """Apply or resume an operator-approved PostgreSQL deployment restore."""
    root = backup_root.expanduser().resolve()
    _validate_confirmation_token(confirmation_token)
    manifest = verify_deployment_backup(root)
    if manifest.persistence_backend != "postgres":
        raise RestoreApplyError("PostgreSQL restore apply requires a PostgreSQL backup")
    if not database_url:
        raise RestoreApplyError(
            "PostgreSQL restore apply requires CONVERGE_DATABASE_URL on the restore host"
        )

    archive = root / "database" / "postgres.dump"
    pg_restore, psql = _validate_postgres_archive(archive)
    manifest_hash = _sha256(root / "manifest.json")
    try:
        database_binding = _postgres_target_binding(database_url)
    except RestoreError as exc:
        raise RestoreApplyError(str(exc)) from exc

    journal_path = _journal_path(root, confirmation_token)
    resumed = journal_path.exists() or _link_like(journal_path)
    if resumed:
        journal = _load_journal(journal_path)
        if journal.confirmation_token != confirmation_token:
            raise RestoreApplyError("restore journal confirmation token mismatch")
        if journal.manifest_sha256 != manifest_hash:
            raise RestoreApplyError("backup manifest changed after restore apply started")
        plan = _resume_postgres_plan(
            journal,
            root,
            database_target_binding=database_binding,
        )
        _validate_journal_keys(journal, plan)

        if set(journal.published) == _expected_journal_keys(plan):
            fresh_plan = plan_deployment_restore(
                root,
                control_db_path=control_db_path,
                database_url=database_url,
            )
            if fresh_plan.ready and fresh_plan.confirmation_token == confirmation_token:
                plan = fresh_plan
                journal = _new_journal(
                    confirmation_token=confirmation_token,
                    manifest_hash=manifest_hash,
                    plan=plan,
                )
                _write_journal(journal_path, journal)
                resumed = False
    else:
        plan = plan_deployment_restore(
            root,
            control_db_path=control_db_path,
            database_url=database_url,
        )
        if not plan.ready:
            raise RestoreApplyError("restore preflight is blocked; run restore-plan for evidence")
        if plan.confirmation_token != confirmation_token:
            raise RestoreApplyError(
                "confirmation token does not match the current restore preflight"
            )
        journal = _new_journal(
            confirmation_token=confirmation_token,
            manifest_hash=manifest_hash,
            plan=plan,
        )
        _create_journal(journal_path, journal)

    _restore_filesystem(
        root=root,
        manifest=manifest,
        plan=plan,
        journal=journal,
        journal_path=journal_path,
    )
    _ensure_postgres_database(
        root=root,
        database_url=database_url,
        pg_restore=pg_restore,
        psql=psql,
        manifest_sha256=manifest_hash,
        database_target_binding=database_binding,
        journal=journal,
        journal_path=journal_path,
    )

    return PostgresRestoreApplyResult(
        projects=[project.project_id for project in manifest.projects],
        resumed=resumed,
    )
