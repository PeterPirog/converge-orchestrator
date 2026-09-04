from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import TypedDict
from unittest.mock import patch
from urllib.parse import urlsplit, urlunsplit
from uuid import uuid4

import pytest
from langgraph.graph import END, START, StateGraph

from converge_orchestrator.backup import create_deployment_backup
from converge_orchestrator.persistence import PersistenceBackend, setup_postgres
from converge_orchestrator.registry_postgres import PostgresControlRegistry
from converge_orchestrator.restore import plan_deployment_restore
from converge_orchestrator.restore_postgres import apply_postgres_restore
from converge_orchestrator.workspace_identity import state_store_id, workspace_id

DATABASE_URL = os.environ.get("CONVERGE_TEST_POSTGRES_URL")
pytestmark = pytest.mark.skipif(
    not DATABASE_URL,
    reason="CONVERGE_TEST_POSTGRES_URL is required for PostgreSQL integration tests",
)


class CounterState(TypedDict):
    value: int


def _counter_graph(checkpointer):
    builder = StateGraph(CounterState)
    builder.add_node("increment", lambda state: {"value": state["value"] + 1})
    builder.add_edge(START, "increment")
    builder.add_edge("increment", END)
    return builder.compile(checkpointer=checkpointer)


def _psycopg():
    import psycopg
    from psycopg import sql

    return psycopg, sql


def _database_url(name: str) -> str:
    assert DATABASE_URL is not None
    parsed = urlsplit(DATABASE_URL)
    assert parsed.scheme in {"postgres", "postgresql"}
    return urlunsplit((parsed.scheme, parsed.netloc, f"/{name}", parsed.query, ""))


@contextmanager
def _isolated_databases() -> Iterator[tuple[str, str]]:
    psycopg, sql = _psycopg()
    source_name = f"converge_src_{uuid4().hex[:12]}"
    target_name = f"converge_dst_{uuid4().hex[:12]}"
    admin_url = _database_url("postgres")
    with psycopg.connect(admin_url, autocommit=True) as db:
        db.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(source_name)))
        db.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(target_name)))
    try:
        yield _database_url(source_name), _database_url(target_name)
    finally:
        with psycopg.connect(admin_url, autocommit=True) as db:
            db.execute(
                sql.SQL("DROP DATABASE IF EXISTS {} WITH (FORCE)").format(
                    sql.Identifier(source_name)
                )
            )
            db.execute(
                sql.SQL("DROP DATABASE IF EXISTS {} WITH (FORCE)").format(
                    sql.Identifier(target_name)
                )
            )


def _lost_postgres_deployment(tmp_path: Path, source_url: str):
    repo = tmp_path / "repository"
    repo.mkdir()
    subprocess.run(["git", "init", "-b", "main", str(repo)], check=True, capture_output=True)
    subprocess.run(
        ["git", "-C", str(repo), "config", "user.email", "ci@example.invalid"],
        check=True,
    )
    subprocess.run(
        ["git", "-C", str(repo), "config", "user.name", "Converge CI"],
        check=True,
    )
    (repo / "README.md").write_text("fixture\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(repo), "add", "README.md"], check=True)
    subprocess.run(
        ["git", "-C", str(repo), "commit", "-m", "fixture"],
        check=True,
        capture_output=True,
    )

    requirements = tmp_path / "architecture.md"
    requirements.write_text("# Architecture\n\nKeep intent immutable.\n", encoding="utf-8")
    state_dir = tmp_path / ".converge-project"
    state_dir.mkdir()
    worktree_dir = state_dir / "worktrees"
    worktree_dir.mkdir()
    (state_dir / "evidence.json").write_text('{"ok": true}\n', encoding="utf-8")

    config = tmp_path / "project.yaml"
    config.write_text(
        "\n".join(
            [
                f'repo_path: "{repo}"',
                f'requirements_path: "{requirements}"',
                f'state_dir: "{state_dir}"',
                f'worktree_dir: "{worktree_dir}"',
                "require_spec_read_only: false",
                "agents:",
                "  planner:",
                "    agent: converge-planner",
                "  builder:",
                "    agent: converge-builder",
                "  reviewer:",
                "    agent: converge-reviewer",
                "quality_gates: []",
                "",
            ]
        ),
        encoding="utf-8",
    )

    setup_postgres(source_url)
    registry = PostgresControlRegistry(source_url)
    project_id = f"fixture-{uuid4().hex[:8]}"
    workspace = workspace_id(repo)
    state_store = state_store_id(state_dir)
    registry.register_project(
        project_id,
        config,
        workspace_id=workspace,
        state_store_id=state_store,
    )
    run_id = f"run-{uuid4().hex}"
    registry.create_run(run_id, project_id, f"thread-{uuid4().hex}")
    registry.update_run(run_id, status="completed", finished=True)

    graph_thread = f"checkpoint-{uuid4().hex}"
    backend = PersistenceBackend(tmp_path / "unused.sqlite", database_url=source_url)
    checkpointer, db = backend.open_checkpointer(state_dir)
    try:
        graph = _counter_graph(checkpointer)
        result = graph.invoke(
            {"value": 41},
            config={"configurable": {"thread_id": graph_thread}},
        )
        assert result["value"] == 42
    finally:
        db.close()

    backup = tmp_path / "backup"
    create_deployment_backup(
        registry=registry,
        persistence_backend="postgres",
        control_db_path=tmp_path / "unused-control.sqlite",
        database_url=source_url,
        destination=backup,
    )

    shutil.rmtree(repo)
    shutil.rmtree(state_dir)
    config.unlink()
    requirements.unlink()
    return {
        "backup": backup,
        "repo": repo,
        "state_dir": state_dir,
        "config": config,
        "requirements": requirements,
        "project_id": project_id,
        "workspace_id": workspace,
        "state_store_id": state_store,
        "run_id": run_id,
        "graph_thread": graph_thread,
    }


def _ready_plan(fixture: dict, target_url: str, tmp_path: Path):
    plan = plan_deployment_restore(
        fixture["backup"],
        control_db_path=tmp_path / "unused-control.sqlite",
        database_url=target_url,
    )
    assert plan.ready is True, plan.blockers
    return plan


def test_postgres_restore_apply_rebuilds_database_filesystem_and_checkpoint(
    tmp_path: Path,
) -> None:
    with _isolated_databases() as (source_url, target_url):
        fixture = _lost_postgres_deployment(tmp_path, source_url)
        plan = _ready_plan(fixture, target_url, tmp_path)

        result = apply_postgres_restore(
            fixture["backup"],
            confirmation_token=plan.confirmation_token,
            control_db_path=tmp_path / "unused-control.sqlite",
            database_url=target_url,
        )

        assert result.persistence_backend == "postgres"
        assert result.resumed is False
        assert fixture["repo"].is_dir()
        assert fixture["state_dir"].is_dir()
        assert fixture["config"].is_file()
        assert fixture["requirements"].is_file()

        registry = PostgresControlRegistry(target_url)
        project = registry.get_project(fixture["project_id"])
        assert project["workspace_id"] == fixture["workspace_id"]
        assert project["state_store_id"] == fixture["state_store_id"]
        assert registry.get_run(fixture["run_id"])["finished_at"] is not None

        backend = PersistenceBackend(tmp_path / "unused.sqlite", database_url=target_url)
        checkpointer, db = backend.open_checkpointer(fixture["state_dir"])
        try:
            snapshot = _counter_graph(checkpointer).get_state(
                {"configurable": {"thread_id": fixture["graph_thread"]}}
            )
            assert snapshot.values["value"] == 42
            assert not snapshot.next
        finally:
            db.close()


def test_postgres_restore_recovers_real_process_death_after_database_commit(
    tmp_path: Path,
) -> None:
    with _isolated_databases() as (source_url, target_url):
        fixture = _lost_postgres_deployment(tmp_path, source_url)
        plan = _ready_plan(fixture, target_url, tmp_path)
        child_env = os.environ.copy()
        child_env.update(
            {
                "CONVERGE_RESTORE_TEST_BACKUP": str(fixture["backup"]),
                "CONVERGE_RESTORE_TEST_TOKEN": plan.confirmation_token,
                "CONVERGE_RESTORE_TEST_DATABASE_URL": target_url,
                "CONVERGE_RESTORE_TEST_CONTROL": str(tmp_path / "unused-control.sqlite"),
            }
        )
        child = """
import os
from pathlib import Path
from unittest.mock import patch
from converge_orchestrator import restore_postgres

original = restore_postgres._mark_published

def crash_after_commit(key, journal, journal_path):
    if key == "database":
        raise SystemExit(37)
    return original(key, journal, journal_path)

with patch.object(restore_postgres, "_mark_published", side_effect=crash_after_commit):
    restore_postgres.apply_postgres_restore(
        Path(os.environ["CONVERGE_RESTORE_TEST_BACKUP"]),
        confirmation_token=os.environ["CONVERGE_RESTORE_TEST_TOKEN"],
        control_db_path=Path(os.environ["CONVERGE_RESTORE_TEST_CONTROL"]),
        database_url=os.environ["CONVERGE_RESTORE_TEST_DATABASE_URL"],
    )
"""
        crashed = subprocess.run(
            [sys.executable, "-c", child],
            env=child_env,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            timeout=1800,
            check=False,
        )
        assert crashed.returncode == 37, crashed.stdout

        journals = list(tmp_path.glob(".backup.converge-restore-*.json"))
        assert len(journals) == 1
        journal = json.loads(journals[0].read_text(encoding="utf-8"))
        assert "database" not in journal["published"]

        with patch(
            "converge_orchestrator.restore_postgres._apply_restore_script",
            side_effect=AssertionError("database restore must be adopted, not repeated"),
        ):
            recovered = apply_postgres_restore(
                fixture["backup"],
                confirmation_token=plan.confirmation_token,
                control_db_path=tmp_path / "unused-control.sqlite",
                database_url=target_url,
            )

        assert recovered.resumed is True
        journal = json.loads(journals[0].read_text(encoding="utf-8"))
        assert "database" in journal["published"]
        assert PostgresControlRegistry(target_url).get_project(fixture["project_id"])[
            "workspace_id"
        ] == fixture["workspace_id"]
