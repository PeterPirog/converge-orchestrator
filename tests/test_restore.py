from __future__ import annotations

import json
import shutil
import sqlite3
import subprocess
from pathlib import Path
from unittest.mock import patch

import pytest

from converge_orchestrator import restore_apply
from converge_orchestrator.backup import create_deployment_backup
from converge_orchestrator.registry import ControlRegistry
from converge_orchestrator.restore import plan_deployment_restore
from converge_orchestrator.workspace_identity import state_store_id, workspace_id


def _lost_sqlite_deployment(tmp_path: Path):
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

    checkpoint = sqlite3.connect(state_dir / "langgraph.sqlite")
    try:
        checkpoint.execute("CREATE TABLE durable(value TEXT)")
        checkpoint.execute("INSERT INTO durable(value) VALUES ('checkpoint')")
        checkpoint.commit()
    finally:
        checkpoint.close()

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
    backup = tmp_path / "backup"
    create_deployment_backup(
        registry=registry,
        persistence_backend="sqlite",
        control_db_path=control_db,
        database_url=None,
        destination=backup,
    )

    shutil.rmtree(repo)
    shutil.rmtree(state_dir)
    config.unlink()
    requirements.unlink()
    control_db.unlink()
    return backup, control_db, repo, state_dir, config, requirements


def _declare_postgres_backup(backup: Path) -> None:
    manifest_path = backup / "manifest.json"
    payload = json.loads(manifest_path.read_text(encoding="utf-8"))
    payload["persistence_backend"] = "postgres"
    manifest_path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")


def test_sqlite_restore_preflight_is_ready_only_after_targets_are_absent(tmp_path: Path) -> None:
    backup, control_db, repo, state_dir, config, requirements = _lost_sqlite_deployment(tmp_path)

    plan = plan_deployment_restore(
        backup,
        control_db_path=control_db,
        database_url=None,
    )

    assert plan.ready is True
    assert plan.blockers == []
    assert plan.persistence_backend == "sqlite"
    assert plan.database_target == str(control_db.resolve())
    assert len(plan.confirmation_token) == 64
    project = plan.projects[0]
    assert project.project_id == "fixture"
    assert project.repo_target == str(repo.resolve())
    assert project.state_target == str(state_dir.resolve())
    assert project.config_target == str(config.resolve())
    assert project.requirements_target == str(requirements.resolve())
    assert project.blockers == []


def test_existing_restore_target_blocks_and_changes_confirmation_token(tmp_path: Path) -> None:
    backup, control_db, repo, _, _, _ = _lost_sqlite_deployment(tmp_path)
    ready = plan_deployment_restore(backup, control_db_path=control_db, database_url=None)
    repo.mkdir()

    blocked = plan_deployment_restore(backup, control_db_path=control_db, database_url=None)

    assert blocked.ready is False
    assert any("repository restore target already exists" in item for item in blocked.blockers)
    assert blocked.confirmation_token != ready.confirmation_token


def test_broken_symlink_restore_target_is_not_treated_as_absent(tmp_path: Path) -> None:
    backup, control_db, repo, _, _, _ = _lost_sqlite_deployment(tmp_path)
    try:
        repo.symlink_to(tmp_path / "missing-repository", target_is_directory=True)
    except OSError:
        pytest.skip("symlinks are not available on this platform")

    plan = plan_deployment_restore(backup, control_db_path=control_db, database_url=None)

    assert plan.ready is False
    assert any("repository restore target already exists" in item for item in plan.blockers)


def test_restore_preflight_validates_manifest_head_against_git_bundle(tmp_path: Path) -> None:
    backup, control_db, _, _, _, _ = _lost_sqlite_deployment(tmp_path)
    manifest_path = backup / "manifest.json"
    payload = json.loads(manifest_path.read_text(encoding="utf-8"))
    payload["projects"][0]["repo_head"] = "0" * 40
    manifest_path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")

    plan = plan_deployment_restore(backup, control_db_path=control_db, database_url=None)

    assert plan.ready is False
    assert any("repository bundle does not contain" in item for item in plan.blockers)


def test_postgres_restore_plan_never_exposes_database_url(tmp_path: Path) -> None:
    backup, control_db, _, _, _, _ = _lost_sqlite_deployment(tmp_path)
    _declare_postgres_backup(backup)
    database_url = "postgresql://user:super-secret@db/converge"

    with (
        patch("converge_orchestrator.restore.shutil.which", return_value="/usr/bin/tool"),
        patch("converge_orchestrator.restore._postgres_target_empty", return_value=True),
        patch(
            "converge_orchestrator.restore._postgres_target_binding",
            return_value="1" * 64,
        ),
    ):
        plan = plan_deployment_restore(
            backup,
            control_db_path=control_db,
            database_url=database_url,
        )

    serialized = plan.model_dump_json()
    assert "super-secret" not in serialized
    assert database_url not in serialized
    assert plan.database_target == "postgres:configured"
    assert plan.ready is False
    assert any("database artifact" in item for item in plan.blockers)


def test_postgres_confirmation_token_changes_when_database_target_changes(tmp_path: Path) -> None:
    backup, control_db, _, _, _, _ = _lost_sqlite_deployment(tmp_path)
    _declare_postgres_backup(backup)
    database_url = "postgresql://user:secret@db/converge"

    with (
        patch("converge_orchestrator.restore.shutil.which", return_value="/usr/bin/tool"),
        patch("converge_orchestrator.restore._postgres_target_empty", return_value=True),
        patch(
            "converge_orchestrator.restore._postgres_target_binding",
            side_effect=["1" * 64, "2" * 64],
        ),
    ):
        first = plan_deployment_restore(
            backup,
            control_db_path=control_db,
            database_url=database_url,
        )
        second = plan_deployment_restore(
            backup,
            control_db_path=control_db,
            database_url=database_url,
        )

    assert first.database_target == second.database_target == "postgres:configured"
    assert first.confirmation_token != second.confirmation_token


def test_sqlite_restore_apply_rebuilds_deployment_and_storage_identity(tmp_path: Path) -> None:
    backup, control_db, repo, state_dir, config, requirements = _lost_sqlite_deployment(tmp_path)
    plan = plan_deployment_restore(backup, control_db_path=control_db, database_url=None)

    result = restore_apply.apply_sqlite_restore(
        backup,
        confirmation_token=plan.confirmation_token,
        control_db_path=control_db,
        database_url=None,
    )

    assert result.status == "restored"
    assert result.resumed is False
    assert result.projects == ["fixture"]
    assert config.is_file()
    assert requirements.is_file()
    assert repo.is_dir()
    assert state_dir.is_dir()
    assert control_db.is_file()

    registry = ControlRegistry(control_db)
    project = registry.get_project("fixture")
    assert workspace_id(repo) == project["workspace_id"]
    assert state_store_id(state_dir) == project["state_store_id"]

    head = subprocess.run(
        ["git", "-C", str(repo), "rev-parse", "HEAD"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    assert head == plan.projects[0].repo_head

    checkpoint = sqlite3.connect(state_dir / "langgraph.sqlite")
    try:
        assert checkpoint.execute("SELECT value FROM durable").fetchone()[0] == "checkpoint"
    finally:
        checkpoint.close()
    assert json.loads((state_dir / "evidence.json").read_text(encoding="utf-8")) == {"ok": True}


def test_sqlite_restore_apply_wrong_token_writes_nothing(tmp_path: Path) -> None:
    backup, control_db, repo, state_dir, config, requirements = _lost_sqlite_deployment(tmp_path)

    with pytest.raises(restore_apply.RestoreApplyError, match="confirmation token"):
        restore_apply.apply_sqlite_restore(
            backup,
            confirmation_token="0" * 64,
            control_db_path=control_db,
            database_url=None,
        )

    assert not control_db.exists()
    assert not repo.exists()
    assert not state_dir.exists()
    assert not config.exists()
    assert not requirements.exists()


def test_sqlite_restore_apply_recovers_publish_before_journal_checkpoint(tmp_path: Path) -> None:
    backup, control_db, repo, state_dir, config, requirements = _lost_sqlite_deployment(tmp_path)
    plan = plan_deployment_restore(backup, control_db_path=control_db, database_url=None)

    with (
        patch.object(restore_apply, "_write_journal", side_effect=SystemExit("process died")),
        pytest.raises(SystemExit, match="process died"),
    ):
        restore_apply.apply_sqlite_restore(
            backup,
            confirmation_token=plan.confirmation_token,
            control_db_path=control_db,
            database_url=None,
        )

    assert requirements.is_file()
    assert not control_db.exists()

    recovered = restore_apply.apply_sqlite_restore(
        backup,
        confirmation_token=plan.confirmation_token,
        control_db_path=control_db,
        database_url=None,
    )

    assert recovered.resumed is True
    assert control_db.is_file()
    assert repo.is_dir()
    assert state_dir.is_dir()
    assert config.is_file()


def test_sqlite_restore_apply_publishes_control_database_last(tmp_path: Path) -> None:
    backup, control_db, _, _, _, _ = _lost_sqlite_deployment(tmp_path)
    plan = plan_deployment_restore(backup, control_db_path=control_db, database_url=None)

    with (
        patch.object(
            restore_apply,
            "_ensure_state",
            side_effect=restore_apply.RestoreApplyError("simulated state failure"),
        ),
        pytest.raises(restore_apply.RestoreApplyError, match="simulated state failure"),
    ):
        restore_apply.apply_sqlite_restore(
            backup,
            confirmation_token=plan.confirmation_token,
            control_db_path=control_db,
            database_url=None,
        )

    assert not control_db.exists()

    recovered = restore_apply.apply_sqlite_restore(
        backup,
        confirmation_token=plan.confirmation_token,
        control_db_path=control_db,
        database_url=None,
    )
    assert recovered.resumed is True
    assert control_db.is_file()
