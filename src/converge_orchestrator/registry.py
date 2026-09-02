from __future__ import annotations

import sqlite3
from datetime import UTC, datetime
from pathlib import Path
from typing import Any


def _now() -> str:
    return datetime.now(UTC).isoformat()


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

    def _initialize(self) -> None:
        with self._connect() as db:
            db.execute("PRAGMA journal_mode = WAL")
            db.executescript(
                """
                CREATE TABLE IF NOT EXISTS projects (
                    id TEXT PRIMARY KEY,
                    config_path TEXT NOT NULL,
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
                    error TEXT
                );

                CREATE INDEX IF NOT EXISTS idx_runs_project ON runs(project_id, started_at);
                CREATE INDEX IF NOT EXISTS idx_runs_task ON runs(active_task_id);
                """
            )

    def register_project(self, project_id: str, config_path: Path) -> dict[str, Any]:
        now = _now()
        resolved = str(config_path.expanduser().resolve())
        with self._connect() as db:
            db.execute(
                """
                INSERT INTO projects(id, config_path, created_at, updated_at)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(id) DO UPDATE SET
                    config_path = excluded.config_path,
                    updated_at = excluded.updated_at
                """,
                (project_id, resolved, now, now),
            )
        return self.get_project(project_id)

    def set_requirements_hash(self, project_id: str, requirements_hash: str) -> None:
        with self._connect() as db:
            cursor = db.execute(
                "UPDATE projects SET requirements_hash = ?, updated_at = ? WHERE id = ?",
                (requirements_hash, _now(), project_id),
            )
            if cursor.rowcount != 1:
                raise KeyError(project_id)

    def get_project(self, project_id: str) -> dict[str, Any]:
        with self._connect() as db:
            row = db.execute("SELECT * FROM projects WHERE id = ?", (project_id,)).fetchone()
        if row is None:
            raise KeyError(project_id)
        return dict(row)

    def list_projects(self) -> list[dict[str, Any]]:
        with self._connect() as db:
            rows = db.execute("SELECT * FROM projects ORDER BY id").fetchall()
        return [dict(row) for row in rows]

    def create_run(self, run_id: str, project_id: str, thread_id: str) -> dict[str, Any]:
        with self._connect() as db:
            db.execute(
                """
                INSERT INTO runs(id, project_id, thread_id, status, started_at)
                VALUES (?, ?, ?, 'queued', ?)
                """,
                (run_id, project_id, thread_id, _now()),
            )
        return self.get_run(run_id)

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
            fields.append("finished_at = ?")
            values.append(_now())
        if not fields:
            return
        values.append(run_id)
        with self._connect() as db:
            cursor = db.execute(
                f"UPDATE runs SET {', '.join(fields)} WHERE id = ?",  # noqa: S608
                values,
            )
            if cursor.rowcount != 1:
                raise KeyError(run_id)

    def get_run(self, run_id: str) -> dict[str, Any]:
        with self._connect() as db:
            row = db.execute("SELECT * FROM runs WHERE id = ?", (run_id,)).fetchone()
        if row is None:
            raise KeyError(run_id)
        return dict(row)

    def runs_for_project(self, project_id: str) -> list[dict[str, Any]]:
        with self._connect() as db:
            rows = db.execute(
                "SELECT * FROM runs WHERE project_id = ? ORDER BY started_at DESC",
                (project_id,),
            ).fetchall()
        return [dict(row) for row in rows]
