from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from typing import Any


def _utcnow() -> datetime:
    return datetime.now(UTC)


def _now() -> str:
    return _utcnow().isoformat()


def _psycopg():
    try:
        import psycopg
        from psycopg.rows import dict_row
    except ImportError as exc:  # pragma: no cover - exercised without optional dependency
        raise RuntimeError(
            "PostgreSQL persistence requires `pip install 'converge-orchestrator[postgres]'`"
        ) from exc
    return psycopg, dict_row


class PostgresControlRegistry:
    """Shared project/run registry with the same contract as the SQLite registry."""

    def __init__(self, database_url: str):
        if not database_url.strip():
            raise ValueError("database_url must not be empty")
        self.database_url = database_url
        self._initialize()

    def _connect(self):
        psycopg, dict_row = _psycopg()
        return psycopg.connect(
            self.database_url,
            autocommit=False,
            row_factory=dict_row,
        )

    @contextmanager
    def _connection(self) -> Iterator[Any]:
        db = self._connect()
        try:
            with db:
                yield db
        finally:
            db.close()

    def _initialize(self) -> None:
        # These statements are idempotent and intentionally small. LangGraph checkpoint migrations
        # are handled separately by `converge persistence-setup`.
        with self._connection() as db:
            db.execute(
                """
                CREATE TABLE IF NOT EXISTS converge_projects (
                    id TEXT PRIMARY KEY,
                    config_path TEXT NOT NULL,
                    workspace_id TEXT,
                    state_store_id TEXT,
                    requirements_hash TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                )
                """
            )
            db.execute(
                """
                ALTER TABLE converge_projects
                ADD COLUMN IF NOT EXISTS workspace_id TEXT
                """
            )
            db.execute(
                """
                ALTER TABLE converge_projects
                ADD COLUMN IF NOT EXISTS state_store_id TEXT
                """
            )
            db.execute(
                """
                CREATE UNIQUE INDEX IF NOT EXISTS idx_converge_projects_state_store_unique
                ON converge_projects(state_store_id)
                WHERE state_store_id IS NOT NULL
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
                    started_at TEXT NOT NULL,
                    finished_at TEXT,
                    error TEXT,
                    lease_owner TEXT,
                    lease_expires_at TEXT
                )
                """
            )
            db.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_converge_runs_project
                ON converge_runs(project_id, started_at)
                """
            )
            db.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_converge_runs_task
                ON converge_runs(active_task_id)
                """
            )

    def _state_store_owner(self, state_store_id: str) -> str | None:
        with self._connection() as db:
            row = db.execute(
                "SELECT id FROM converge_projects WHERE state_store_id = %s",
                (state_store_id,),
            ).fetchone()
        return str(row["id"]) if row is not None else None

    def register_project(
        self,
        project_id: str,
        config_path: Any,
        workspace_id: str | None = None,
        state_store_id: str | None = None,
    ) -> dict[str, Any]:
        now = _now()
        resolved = str(config_path.expanduser().resolve())
        psycopg, _ = _psycopg()
        try:
            with self._connection() as db:
                cursor = db.execute(
                    """
                    INSERT INTO converge_projects(
                        id, config_path, workspace_id, state_store_id, created_at, updated_at
                    )
                    VALUES (%s, %s, %s, %s, %s, %s)
                    ON CONFLICT(id) DO UPDATE SET
                        config_path = EXCLUDED.config_path,
                        workspace_id = COALESCE(
                            EXCLUDED.workspace_id, converge_projects.workspace_id
                        ),
                        state_store_id = COALESCE(
                            EXCLUDED.state_store_id, converge_projects.state_store_id
                        ),
                        updated_at = EXCLUDED.updated_at
                    WHERE (EXCLUDED.workspace_id IS NULL
                           OR converge_projects.workspace_id IS NULL
                           OR converge_projects.workspace_id = EXCLUDED.workspace_id)
                      AND (EXCLUDED.state_store_id IS NULL
                           OR converge_projects.state_store_id IS NULL
                           OR converge_projects.state_store_id = EXCLUDED.state_store_id)
                    """,
                    (project_id, resolved, workspace_id, state_store_id, now, now),
                )
                if cursor.rowcount != 1:
                    existing = db.execute(
                        """
                        SELECT workspace_id, state_store_id
                        FROM converge_projects
                        WHERE id = %s
                        """,
                        (project_id,),
                    ).fetchone()
                    if existing is None:
                        raise KeyError(project_id)
                    if workspace_id is not None and existing["workspace_id"] not in (
                        None,
                        workspace_id,
                    ):
                        raise ValueError(
                            f"Project {project_id} is already bound to workspace "
                            f"{existing['workspace_id']}; refusing registration from workspace "
                            f"{workspace_id}"
                        )
                    raise ValueError(
                        f"Project {project_id} is already bound to state store "
                        f"{existing['state_store_id']}; refusing registration from state store "
                        f"{state_store_id}"
                    )
        except psycopg.errors.UniqueViolation as exc:
            if state_store_id is None:
                raise
            owner = self._state_store_owner(state_store_id)
            if owner is None or owner == project_id:
                raise
            raise ValueError(
                f"State store {state_store_id} is already assigned to project {owner}; "
                f"project {project_id} must use its own state_dir"
            ) from exc
        return self.get_project(project_id)

    def set_requirements_hash(self, project_id: str, requirements_hash: str) -> None:
        with self._connection() as db:
            cursor = db.execute(
                """
                UPDATE converge_projects
                SET requirements_hash = %s, updated_at = %s
                WHERE id = %s
                """,
                (requirements_hash, _now(), project_id),
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
        return dict(row)

    def list_projects(self) -> list[dict[str, Any]]:
        with self._connection() as db:
            rows = db.execute("SELECT * FROM converge_projects ORDER BY id").fetchall()
        return [dict(row) for row in rows]

    def create_run(self, run_id: str, project_id: str, thread_id: str) -> dict[str, Any]:
        with self._connection() as db:
            db.execute(
                """
                INSERT INTO converge_runs(id, project_id, thread_id, status, started_at)
                VALUES (%s, %s, %s, 'queued', %s)
                """,
                (run_id, project_id, thread_id, _now()),
            )
        return self.get_run(run_id)

    def claim_run_lease(self, run_id: str, owner: str, ttl_seconds: int) -> bool:
        if ttl_seconds <= 0:
            raise ValueError("ttl_seconds must be positive")
        now = _utcnow()
        expires_at = (now + timedelta(seconds=ttl_seconds)).isoformat()
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
            raw_expiry = row["lease_expires_at"]
            expiry = datetime.fromisoformat(raw_expiry) if raw_expiry else None
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
        expires_at = (_utcnow() + timedelta(seconds=ttl_seconds)).isoformat()
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
            values.append(_now())
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
        return dict(row)

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
        return [dict(row) for row in rows]
