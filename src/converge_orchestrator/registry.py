from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any


def _utcnow() -> datetime:
    return datetime.now(UTC)


def _now() -> str:
    return _utcnow().isoformat()


class ControlRegistry:
    """Durable project/run metadata independent from LangGraph checkpoints."""

    def __init__(self, path: Path):
        self.path = path.expanduser().resolve()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._initialize()

    def _connect(self) -> sqlite3.Connection:
        db = sqlite3.connect(self.path, timeout=30)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA foreign_keys = ON")
        return db

    @contextmanager
    def _connection(self) -> Iterator[sqlite3.Connection]:
        """Commit/rollback transaction scope and always release the SQLite handle."""
        db = self._connect()
        try:
            with db:
                yield db
        finally:
            db.close()

    def _initialize(self) -> None:
        with self._connection() as db:
            db.execute("PRAGMA journal_mode = WAL")
            db.executescript(
                """
                CREATE TABLE IF NOT EXISTS projects (
                    id TEXT PRIMARY KEY,
                    config_path TEXT NOT NULL,
                    workspace_id TEXT,
                    state_store_id TEXT,
                    requirements_hash TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS runs (
                    id TEXT PRIMARY KEY,
                    project_id TEXT NOT NULL REFERENCES projects(id),
                    thread_id TEXT NOT NULL UNIQUE,
                    status TEXT NOT NULL,
                    node TEXT,
                    active_task_id TEXT,
                    started_at TEXT NOT NULL,
                    finished_at TEXT,
                    error TEXT,
                    lease_owner TEXT,
                    lease_expires_at TEXT
                );

                CREATE INDEX IF NOT EXISTS idx_runs_project ON runs(project_id, started_at);
                CREATE INDEX IF NOT EXISTS idx_runs_task ON runs(active_task_id);
                """
            )
            project_columns = {
                str(row["name"])
                for row in db.execute("PRAGMA table_info(projects)").fetchall()
            }
            if "workspace_id" not in project_columns:
                db.execute("ALTER TABLE projects ADD COLUMN workspace_id TEXT")
            if "state_store_id" not in project_columns:
                db.execute("ALTER TABLE projects ADD COLUMN state_store_id TEXT")
            db.execute(
                """
                CREATE UNIQUE INDEX IF NOT EXISTS idx_projects_state_store_unique
                ON projects(state_store_id)
                WHERE state_store_id IS NOT NULL
                """
            )

            run_columns = {
                str(row["name"])
                for row in db.execute("PRAGMA table_info(runs)").fetchall()
            }
            if "lease_owner" not in run_columns:
                db.execute("ALTER TABLE runs ADD COLUMN lease_owner TEXT")
            if "lease_expires_at" not in run_columns:
                db.execute("ALTER TABLE runs ADD COLUMN lease_expires_at TEXT")

    def _state_store_owner(self, state_store_id: str) -> str | None:
        with self._connection() as db:
            row = db.execute(
                "SELECT id FROM projects WHERE state_store_id = ?",
                (state_store_id,),
            ).fetchone()
        return str(row["id"]) if row is not None else None

    def register_project(
        self,
        project_id: str,
        config_path: Path,
        workspace_id: str | None = None,
        state_store_id: str | None = None,
    ) -> dict[str, Any]:
        now = _now()
        resolved = str(config_path.expanduser().resolve())
        try:
            with self._connection() as db:
                cursor = db.execute(
                    """
                    INSERT INTO projects(
                        id, config_path, workspace_id, state_store_id, created_at, updated_at
                    )
                    VALUES (?, ?, ?, ?, ?, ?)
                    ON CONFLICT(id) DO UPDATE SET
                        config_path = excluded.config_path,
                        workspace_id = COALESCE(excluded.workspace_id, projects.workspace_id),
                        state_store_id = COALESCE(excluded.state_store_id, projects.state_store_id),
                        updated_at = excluded.updated_at
                    WHERE (excluded.workspace_id IS NULL
                           OR projects.workspace_id IS NULL
                           OR projects.workspace_id = excluded.workspace_id)
                      AND (excluded.state_store_id IS NULL
                           OR projects.state_store_id IS NULL
                           OR projects.state_store_id = excluded.state_store_id)
                    """,
                    (project_id, resolved, workspace_id, state_store_id, now, now),
                )
                if cursor.rowcount != 1:
                    existing = db.execute(
                        "SELECT workspace_id, state_store_id FROM projects WHERE id = ?",
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
        except sqlite3.IntegrityError as exc:
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
                "UPDATE projects SET requirements_hash = ?, updated_at = ? WHERE id = ?",
                (requirements_hash, _now(), project_id),
            )
            if cursor.rowcount != 1:
                raise KeyError(project_id)

    def get_project(self, project_id: str) -> dict[str, Any]:
        with self._connection() as db:
            row = db.execute("SELECT * FROM projects WHERE id = ?", (project_id,)).fetchone()
        if row is None:
            raise KeyError(project_id)
        return dict(row)

    def list_projects(self) -> list[dict[str, Any]]:
        with self._connection() as db:
            rows = db.execute("SELECT * FROM projects ORDER BY id").fetchall()
        return [dict(row) for row in rows]

    def create_run(self, run_id: str, project_id: str, thread_id: str) -> dict[str, Any]:
        with self._connection() as db:
            db.execute(
                """
                INSERT INTO runs(id, project_id, thread_id, status, started_at)
                VALUES (?, ?, ?, 'queued', ?)
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
            db.execute("BEGIN IMMEDIATE")
            row = db.execute(
                "SELECT finished_at, lease_owner, lease_expires_at FROM runs WHERE id = ?",
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
                "UPDATE runs SET lease_owner = ?, lease_expires_at = ? WHERE id = ?",
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
                UPDATE runs
                SET lease_expires_at = ?
                WHERE id = ? AND lease_owner = ? AND finished_at IS NULL
                """,
                (expires_at, run_id, owner),
            )
        return cursor.rowcount == 1

    def release_run_lease(self, run_id: str, owner: str) -> bool:
        with self._connection() as db:
            cursor = db.execute(
                """
                UPDATE runs
                SET lease_owner = NULL, lease_expires_at = NULL
                WHERE id = ? AND lease_owner = ?
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
            fields.append("status = ?")
            values.append(status)
        if node is not None:
            fields.append("node = ?")
            values.append(node)
        if active_task_id is not None:
            fields.append("active_task_id = ?")
            values.append(active_task_id)
        if error is not None:
            fields.append("error = ?")
            values.append(error)
        if finished:
            fields.extend(
                [
                    "finished_at = ?",
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
                f"UPDATE runs SET {', '.join(fields)} WHERE id = ?",  # noqa: S608
                values,
            )
            if cursor.rowcount != 1:
                raise KeyError(run_id)

    def get_run(self, run_id: str) -> dict[str, Any]:
        with self._connection() as db:
            row = db.execute("SELECT * FROM runs WHERE id = ?", (run_id,)).fetchone()
        if row is None:
            raise KeyError(run_id)
        return dict(row)

    def runs_for_project(self, project_id: str) -> list[dict[str, Any]]:
        with self._connection() as db:
            rows = db.execute(
                "SELECT * FROM runs WHERE project_id = ? ORDER BY started_at DESC",
                (project_id,),
            ).fetchall()
        return [dict(row) for row in rows]
