from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from psycopg import Connection
from psycopg.rows import dict_row


def _utcnow() -> datetime:
    return datetime.now(UTC)


def _record(row: Any) -> dict[str, Any]:
    result = dict(row)
    for key in ("created_at", "updated_at", "started_at", "finished_at", "lease_expires_at"):
        value = result.get(key)
        if isinstance(value, datetime):
            result[key] = value.isoformat()
    return result


class PostgresControlRegistry:
    """Shared durable control registry for multi-process service coordination."""

    def __init__(self, dsn: str):
        if not dsn.strip():
            raise ValueError("PostgreSQL DSN must not be empty")
        self._dsn = dsn
        self._initialize()

    def _connect(self) -> Connection:
        return Connection.connect(self._dsn, row_factory=dict_row)

    @contextmanager
    def _connection(self) -> Iterator[Connection]:
        connection = self._connect()
        try:
            with connection.transaction():
                yield connection
        finally:
            connection.close()

    def _initialize(self) -> None:
        with self._connection() as db:
            db.execute(
                """
                CREATE TABLE IF NOT EXISTS converge_projects (
                    id TEXT PRIMARY KEY,
                    config_path TEXT NOT NULL,
                    requirements_hash TEXT,
                    created_at TIMESTAMPTZ NOT NULL,
                    updated_at TIMESTAMPTZ NOT NULL
                )
                """
            )
            db.execute(
                """
                CREATE TABLE IF NOT EXISTS converge_runs (
                    id TEXT PRIMARY KEY,
                    project_id TEXT NOT NULL REFERENCES converge_projects(id),
                    thread_id TEXT NOT NULL UNIQUE,
                    status TEXT NOT NULL,
                    node TEXT,
                    active_task_id TEXT,
                    started_at TIMESTAMPTZ NOT NULL,
                    finished_at TIMESTAMPTZ,
                    error TEXT,
                    lease_owner TEXT,
                    lease_expires_at TIMESTAMPTZ
                )
                """
            )
            db.execute(
                "CREATE INDEX IF NOT EXISTS idx_converge_runs_project "
                "ON converge_runs(project_id, started_at)"
            )
            db.execute(
                "CREATE INDEX IF NOT EXISTS idx_converge_runs_task "
                "ON converge_runs(active_task_id)"
            )

    def register_project(self, project_id: str, config_path: Path) -> dict[str, Any]:
        now = _utcnow()
        resolved = str(config_path.expanduser().resolve())
        with self._connection() as db:
            db.execute(
                """
                INSERT INTO converge_projects(id, config_path, created_at, updated_at)
                VALUES (%s, %s, %s, %s)
                ON CONFLICT(id) DO UPDATE SET
                    config_path = excluded.config_path,
                    updated_at = excluded.updated_at
                """,
                (project_id, resolved, now, now),
            )
        return self.get_project(project_id)

    def set_requirements_hash(self, project_id: str, requirements_hash: str) -> None:
        with self._connection() as db:
            cursor = db.execute(
                """
                UPDATE converge_projects
                SET requirements_hash = %s, updated_at = %s
                WHERE id = %s
                """,
                (requirements_hash, _utcnow(), project_id),
            )
            if cursor.rowcount != 1:
                raise KeyError(project_id)

    def get_project(self, project_id: str) -> dict[str, Any]:
        with self._connection() as db:
            row = db.execute(
                "SELECT * FROM converge_projects WHERE id = %s",
                (project_id,),
            ).fetchone()
        if row is None:
            raise KeyError(project_id)
        return _record(row)

    def list_projects(self) -> list[dict[str, Any]]:
        with self._connection() as db:
            rows = db.execute("SELECT * FROM converge_projects ORDER BY id").fetchall()
        return [_record(row) for row in rows]

    def create_run(self, run_id: str, project_id: str, thread_id: str) -> dict[str, Any]:
        with self._connection() as db:
            db.execute(
                """
                INSERT INTO converge_runs(id, project_id, thread_id, status, started_at)
                VALUES (%s, %s, %s, 'queued', %s)
                """,
                (run_id, project_id, thread_id, _utcnow()),
            )
        return self.get_run(run_id)

    def claim_run_lease(self, run_id: str, owner: str, ttl_seconds: int) -> bool:
        if ttl_seconds <= 0:
            raise ValueError("ttl_seconds must be positive")
        now = _utcnow()
        expires_at = now + timedelta(seconds=ttl_seconds)
        with self._connection() as db:
            row = db.execute(
                """
                SELECT finished_at, lease_owner, lease_expires_at
                FROM converge_runs
                WHERE id = %s
                FOR UPDATE
                """,
                (run_id,),
            ).fetchone()
            if row is None:
                raise KeyError(run_id)
            if row["finished_at"]:
                return False
            current_owner = row["lease_owner"]
            expiry = row["lease_expires_at"]
            if current_owner and current_owner != owner and expiry and expiry > now:
                return False
            db.execute(
                """
                UPDATE converge_runs
                SET lease_owner = %s, lease_expires_at = %s
                WHERE id = %s
                """,
                (owner, expires_at, run_id),
            )
        return True

    def renew_run_lease(self, run_id: str, owner: str, ttl_seconds: int) -> bool:
        if ttl_seconds <= 0:
            raise ValueError("ttl_seconds must be positive")
        expires_at = _utcnow() + timedelta(seconds=ttl_seconds)
        with self._connection() as db:
            cursor = db.execute(
                """
                UPDATE converge_runs
                SET lease_expires_at = %s
                WHERE id = %s AND lease_owner = %s AND finished_at IS NULL
                """,
                (expires_at, run_id, owner),
            )
        return cursor.rowcount == 1

    def release_run_lease(self, run_id: str, owner: str) -> bool:
        with self._connection() as db:
            cursor = db.execute(
                """
                UPDATE converge_runs
                SET lease_owner = NULL, lease_expires_at = NULL
                WHERE id = %s AND lease_owner = %s
                """,
                (run_id, owner),
            )
        return cursor.rowcount == 1

    def update_run(
        self,
        run_id: str,
        *,
        status: str | None = None,
        node: str | None = None,
        active_task_id: str | None = None,
        error: str | None = None,
        finished: bool = False,
    ) -> None:
        fields: list[str] = []
        values: list[Any] = []
        if status is not None:
            fields.append("status = %s")
            values.append(status)
        if node is not None:
            fields.append("node = %s")
            values.append(node)
        if active_task_id is not None:
            fields.append("active_task_id = %s")
            values.append(active_task_id)
        if error is not None:
            fields.append("error = %s")
            values.append(error)
        if finished:
            fields.extend(
                [
                    "finished_at = %s",
                    "lease_owner = NULL",
                    "lease_expires_at = NULL",
                ]
            )
            values.append(_utcnow())
        if not fields:
            return
        values.append(run_id)
        with self._connection() as db:
            cursor = db.execute(
                f"UPDATE converge_runs SET {', '.join(fields)} WHERE id = %s",  # noqa: S608
                values,
            )
            if cursor.rowcount != 1:
                raise KeyError(run_id)

    def get_run(self, run_id: str) -> dict[str, Any]:
        with self._connection() as db:
            row = db.execute(
                "SELECT * FROM converge_runs WHERE id = %s",
                (run_id,),
            ).fetchone()
        if row is None:
            raise KeyError(run_id)
        return _record(row)

    def runs_for_project(self, project_id: str) -> list[dict[str, Any]]:
        with self._connection() as db:
            rows = db.execute(
                """
                SELECT * FROM converge_runs
                WHERE project_id = %s
                ORDER BY started_at DESC
                """,
                (project_id,),
            ).fetchall()
        return [_record(row) for row in rows]
