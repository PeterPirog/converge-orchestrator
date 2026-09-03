import sqlite3
from pathlib import Path

import pytest

from converge_orchestrator.registry import ControlRegistry


def test_registry_persists_projects_and_runs(tmp_path: Path) -> None:
    registry = ControlRegistry(tmp_path / "control.sqlite")
    config_path = tmp_path / "project.yaml"
    project = registry.register_project("payments", config_path)
    assert project["id"] == "payments"
    assert project["config_path"] == str(config_path.resolve())

    run = registry.create_run("run-1", "payments", "thread-1")
    assert run["status"] == "queued"
    registry.update_run(
        "run-1",
        status="interrupted",
        node="human",
        active_task_id="ARCH-017-1",
    )
    restored = registry.get_run("run-1")
    assert restored["node"] == "human"
    assert restored["active_task_id"] == "ARCH-017-1"


def test_registry_connection_scope_closes_handle(tmp_path: Path) -> None:
    registry = ControlRegistry(tmp_path / "control.sqlite")

    with registry._connection() as db:
        assert db.execute("SELECT 1").fetchone()[0] == 1

    with pytest.raises(sqlite3.ProgrammingError, match="closed database"):
        db.execute("SELECT 1")


def test_registry_connection_scope_rolls_back_before_close(tmp_path: Path) -> None:
    registry = ControlRegistry(tmp_path / "control.sqlite")

    with pytest.raises(RuntimeError, match="abort transaction"):
        with registry._connection() as db:
            db.execute(
                """
                INSERT INTO projects(id, config_path, created_at, updated_at)
                VALUES ('temporary', '/tmp/project.yaml', 'now', 'now')
                """
            )
            raise RuntimeError("abort transaction")

    assert registry.list_projects() == []
