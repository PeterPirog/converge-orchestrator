from __future__ import annotations

from pathlib import Path
from unittest.mock import Mock, patch

import pytest
import yaml

from converge_orchestrator.config import (
    load_config,
    load_run_config_snapshot,
    materialize_run_config_snapshot,
)
from converge_orchestrator.runtime import RunController


def _write_config(tmp_path: Path, *, max_repairs: int) -> Path:
    repo = tmp_path / "repository"
    repo.mkdir(exist_ok=True)
    requirements = tmp_path / "architecture.md"
    requirements.write_text("# Architecture\n\n- ARCH-001 Keep behavior stable.\n", encoding="utf-8")
    state_dir = tmp_path / ".converge"
    config = tmp_path / "converge.yaml"
    config.write_text(
        yaml.safe_dump(
            {
                "version": 1,
                "project": {
                    "name": "fixture",
                    "repo_path": str(repo),
                    "requirements_path": str(requirements),
                    "state_dir": str(state_dir),
                    "worktree_dir": str(state_dir / "worktrees"),
                    "require_spec_read_only": False,
                },
                "agents": {},
                "workflow": {"max_repair_attempts": max_repairs},
            },
            sort_keys=True,
        ),
        encoding="utf-8",
    )
    return config


def test_source_config_changes_only_affect_future_runs(tmp_path: Path) -> None:
    source = _write_config(tmp_path, max_repairs=1)
    _, snapshot, digest = materialize_run_config_snapshot(source, "run-1")

    source = _write_config(tmp_path, max_repairs=9)

    assert load_config(source).max_repair_attempts == 9
    assert load_run_config_snapshot(snapshot, digest).max_repair_attempts == 1


def test_runtime_reloads_active_run_from_pinned_snapshot(tmp_path: Path) -> None:
    source = _write_config(tmp_path, max_repairs=1)
    _, snapshot, digest = materialize_run_config_snapshot(source, "run-1")
    _write_config(tmp_path, max_repairs=9)

    project = {
        "id": "fixture",
        "config_path": str(source),
        "workspace_id": "workspace",
        "state_store_id": "state-store",
    }
    record = {
        "id": "run-1",
        "project_id": "fixture",
        "config_snapshot_path": str(snapshot),
        "config_snapshot_sha256": digest,
    }
    controller = object.__new__(RunController)
    controller.registry = Mock()
    controller.registry.get_project.return_value = project

    with (
        patch("converge_orchestrator.runtime.assert_workspace_affinity"),
        patch("converge_orchestrator.runtime.assert_state_store_affinity"),
    ):
        cfg = controller._config_for_run(record)

    assert cfg.max_repair_attempts == 1
    assert controller._config_path_for_run(record) == str(snapshot.resolve())


def test_tampered_run_config_snapshot_fails_closed(tmp_path: Path) -> None:
    source = _write_config(tmp_path, max_repairs=1)
    _, snapshot, digest = materialize_run_config_snapshot(source, "run-1")
    snapshot.write_text(snapshot.read_text(encoding="utf-8") + "# tampered\n", encoding="utf-8")

    with pytest.raises(RuntimeError, match="Pinned run configuration changed"):
        load_run_config_snapshot(snapshot, digest)


def test_incomplete_run_config_metadata_fails_closed() -> None:
    controller = object.__new__(RunController)
    controller.registry = Mock()
    record = {
        "id": "run-1",
        "project_id": "fixture",
        "config_snapshot_path": "/tmp/run.yaml",
        "config_snapshot_sha256": None,
    }

    with pytest.raises(RuntimeError, match="incomplete pinned configuration metadata"):
        controller._config_for_run(record)
