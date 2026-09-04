from __future__ import annotations

import sqlite3
import subprocess
from pathlib import Path
from unittest.mock import patch

import pytest

from converge_orchestrator.backup import (
    BackupError,
    _postgres_backup,
    create_deployment_backup,
    verify_deployment_backup,
)
from converge_orchestrator.registry import ControlRegistry
from converge_orchestrator.workspace_identity import state_store_id, workspace_id
from converge_orchestrator.workspace_ownership import WorkspaceOwnershipStore


def _deployment(tmp_path: Path):
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
    requirements.write_text("# Architecture\n\nKeep the system deterministic.\n", encoding="utf-8")
    state_dir = tmp_path / ".converge-project"
    state_dir.mkdir()
    worktree_dir = state_dir / "worktrees"
    worktree_dir.mkdir()
    (state_dir / "evidence.json").write_text('{"ok": true}\n', encoding="utf-8")

    langgraph = state_dir / "langgraph.sqlite"
    db = sqlite3.connect(langgraph)
    try:
        db.execute("CREATE TABLE checkpoint_fixture(value TEXT)")
        db.execute("INSERT INTO checkpoint_fixture(value) VALUES ('durable')")
        db.commit()
    finally:
        db.close()

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

    control_db = tmp_path / "control.sqlite"
    registry = ControlRegistry(control_db)
    registry.register_project(
        "fixture",
        config,
        workspace_id=workspace_id(repo),
        state_store_id=state_store_id(state_dir),
    )
    return registry, control_db, repo, state_dir, worktree_dir


def test_sqlite_backup_is_quiescent_complete_and_verifiable(tmp_path: Path) -> None:
    registry, control_db, repo, _, _ = _deployment(tmp_path)
    destination = tmp_path / "backups" / "snapshot-1"

    manifest = create_deployment_backup(
        registry=registry,
        persistence_backend="sqlite",
        control_db_path=control_db,
        database_url=None,
        destination=destination,
    )

    assert manifest.persistence_backend == "sqlite"
    assert [project.project_id for project in manifest.projects] == ["fixture"]
    assert (destination / "manifest.json").is_file()
    assert (destination / "database" / "control.sqlite").is_file()
    assert (destination / "projects" / "fixture" / "repository.bundle").is_file()
    assert (destination / "projects" / "fixture" / "state" / "evidence.json").is_file()
    assert (destination / "projects" / "fixture" / "langgraph.sqlite").is_file()
    assert not (destination / "projects" / "fixture" / "state" / "worktrees").exists()

    verified = verify_deployment_backup(destination)
    assert verified == manifest

    bundle = destination / "projects" / "fixture" / "repository.bundle"
    result = subprocess.run(
        ["git", "bundle", "verify", str(bundle)],
        cwd=repo,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        check=False,
    )
    assert result.returncode == 0

    checkpoint = sqlite3.connect(destination / "projects" / "fixture" / "langgraph.sqlite")
    try:
        row = checkpoint.execute("SELECT value FROM checkpoint_fixture").fetchone()
    finally:
        checkpoint.close()
    assert row == ("durable",)


def test_backup_verification_detects_tampering(tmp_path: Path) -> None:
    registry, control_db, _, _, _ = _deployment(tmp_path)
    destination = tmp_path / "snapshot"
    create_deployment_backup(
        registry=registry,
        persistence_backend="sqlite",
        control_db_path=control_db,
        database_url=None,
        destination=destination,
    )
    target = destination / "projects" / "fixture" / "requirements.md"
    target.write_text("tampered\n", encoding="utf-8")

    with pytest.raises(BackupError, match="mismatch"):
        verify_deployment_backup(destination)


def test_unfinished_run_blocks_backup(tmp_path: Path) -> None:
    registry, control_db, _, _, _ = _deployment(tmp_path)
    registry.create_run("run-active", "fixture", "thread-active")

    with pytest.raises(BackupError, match="unfinished runs"):
        create_deployment_backup(
            registry=registry,
            persistence_backend="sqlite",
            control_db_path=control_db,
            database_url=None,
            destination=tmp_path / "snapshot",
        )


def test_active_worktree_ownership_blocks_backup(tmp_path: Path) -> None:
    registry, control_db, _, _, worktree_dir = _deployment(tmp_path)
    target = worktree_dir / "task-worktree"
    target.mkdir()
    WorkspaceOwnershipStore(worktree_dir).activate(
        task_id="ARCH-001-1",
        target=target,
        branch="converge/ARCH-001-1",
    )

    with pytest.raises(BackupError, match="active or pending-cleanup worktrees"):
        create_deployment_backup(
            registry=registry,
            persistence_backend="sqlite",
            control_db_path=control_db,
            database_url=None,
            destination=tmp_path / "snapshot",
        )


def test_dirty_base_repository_blocks_backup(tmp_path: Path) -> None:
    registry, control_db, repo, _, _ = _deployment(tmp_path)
    (repo / "local.txt").write_text("uncommitted\n", encoding="utf-8")

    with pytest.raises(BackupError, match="uncommitted changes"):
        create_deployment_backup(
            registry=registry,
            persistence_backend="sqlite",
            control_db_path=control_db,
            database_url=None,
            destination=tmp_path / "snapshot",
        )


def test_state_symlink_blocks_backup(tmp_path: Path) -> None:
    registry, control_db, _, state_dir, _ = _deployment(tmp_path)
    outside = tmp_path / "outside.txt"
    outside.write_text("do not follow\n", encoding="utf-8")
    try:
        (state_dir / "link.txt").symlink_to(outside)
    except OSError:
        pytest.skip("symlinks are not available on this platform")

    with pytest.raises(BackupError, match="contains symlink"):
        create_deployment_backup(
            registry=registry,
            persistence_backend="sqlite",
            control_db_path=control_db,
            database_url=None,
            destination=tmp_path / "snapshot",
        )


def test_postgres_dump_keeps_database_url_out_of_argv(tmp_path: Path) -> None:
    completed = subprocess.CompletedProcess(args=[], returncode=0, stdout="")
    client_env = {
        "PGHOST": "database",
        "PGDATABASE": "converge",
        "PGUSER": "user",
        "PGPASSWORD": "super-secret",
    }
    with (
        patch("converge_orchestrator.backup.shutil.which", return_value="/usr/bin/pg_dump"),
        patch("converge_orchestrator.backup.libpq_env", return_value=client_env) as env_builder,
        patch("converge_orchestrator.backup.subprocess.run", return_value=completed) as runner,
    ):
        _postgres_backup(
            "postgresql://user:super-secret@database/converge",
            tmp_path / "postgres.dump",
        )

    env_builder.assert_called_once()
    command = runner.call_args.args[0]
    assert all("super-secret" not in str(part) for part in command)
    assert runner.call_args.kwargs["env"]["PGDATABASE"] == "converge"
    assert runner.call_args.kwargs["env"]["PGPASSWORD"] == "super-secret"
