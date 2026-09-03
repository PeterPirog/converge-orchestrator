from __future__ import annotations

import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import patch

from converge_orchestrator.registry import ControlRegistry


def _registry(tmp_path: Path) -> ControlRegistry:
    registry = ControlRegistry(tmp_path / "control.sqlite")
    registry.register_project("project", tmp_path / "converge.yaml")
    registry.create_run("run-1", "project", "thread-1")
    return registry


def test_active_lease_blocks_second_controller_until_expiry(tmp_path: Path) -> None:
    registry = _registry(tmp_path)
    start = datetime(2026, 1, 1, tzinfo=UTC)

    with patch("converge_orchestrator.registry._utcnow", return_value=start):
        assert registry.claim_run_lease("run-1", "controller-a", 60)

    with patch(
        "converge_orchestrator.registry._utcnow",
        return_value=start + timedelta(seconds=30),
    ):
        assert not registry.claim_run_lease("run-1", "controller-b", 60)

    with patch(
        "converge_orchestrator.registry._utcnow",
        return_value=start + timedelta(seconds=61),
    ):
        assert registry.claim_run_lease("run-1", "controller-b", 60)

    assert registry.get_run("run-1")["lease_owner"] == "controller-b"


def test_lease_can_be_renewed_and_released_only_by_owner(tmp_path: Path) -> None:
    registry = _registry(tmp_path)
    now = datetime(2026, 1, 1, tzinfo=UTC)
    with patch("converge_orchestrator.registry._utcnow", return_value=now):
        assert registry.claim_run_lease("run-1", "controller-a", 60)
        initial_expiry = registry.get_run("run-1")["lease_expires_at"]

    with patch(
        "converge_orchestrator.registry._utcnow",
        return_value=now + timedelta(seconds=20),
    ):
        assert not registry.renew_run_lease("run-1", "controller-b", 60)
        assert registry.renew_run_lease("run-1", "controller-a", 60)

    renewed_expiry = registry.get_run("run-1")["lease_expires_at"]
    assert renewed_expiry > initial_expiry
    assert not registry.release_run_lease("run-1", "controller-b")
    assert registry.release_run_lease("run-1", "controller-a")
    assert registry.get_run("run-1")["lease_owner"] is None


def test_existing_registry_schema_is_migrated_with_lease_columns(tmp_path: Path) -> None:
    path = tmp_path / "legacy.sqlite"
    with sqlite3.connect(path) as db:
        db.executescript(
            """
            CREATE TABLE projects (
                id TEXT PRIMARY KEY,
                config_path TEXT NOT NULL,
                requirements_hash TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            CREATE TABLE runs (
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
            """
        )

    ControlRegistry(path)

    with sqlite3.connect(path) as db:
        columns = {row[1] for row in db.execute("PRAGMA table_info(runs)").fetchall()}
    assert "lease_owner" in columns
    assert "lease_expires_at" in columns
