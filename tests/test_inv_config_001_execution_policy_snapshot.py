"""Regression lock for INV-CONFIG-001 (single hash-pinned execution-policy snapshot).

A started run must keep using the one immutable normalized configuration snapshot that was
materialized at start time -- including after a fresh controller is constructed (process
restart) -- even if the source ``converge.yaml`` has since drifted.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest
import yaml

from converge_orchestrator.config import (
    load_run_config_snapshot,
    materialize_run_config_snapshot,
)
from converge_orchestrator.runtime_service import ScheduledRunController


def _git_init(repo: Path) -> None:
    subprocess.run(["git", "init", "-b", "main", str(repo)], check=True, capture_output=True)


def _write_source_config(config: Path, *, max_repairs: int, mode: str) -> None:
    """Write the raw (un-normalized) source mapping: relative paths + mode/profile_sets."""
    config.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "version": 1,
        "project": {
            "name": "fixture",
            "repo_path": "repository",
            "requirements_path": "architecture.md",
            "state_dir": ".converge",
            "worktree_dir": ".converge/worktrees",
            "require_spec_read_only": False,
        },
        "agents": {},
        "models": {
            "mode": mode,
            "profile_sets": {
                "cloud": {"default": {"model": "cloud-profile"}},
                "local": {"default": {"model": "local-profile"}},
            },
        },
        "workflow": {"max_repair_attempts": max_repairs},
    }
    config.write_text(yaml.safe_dump(payload, sort_keys=True), encoding="utf-8")


def test_started_run_serves_hash_pinned_snapshot_after_restart(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.delenv("CONVERGE_DATABASE_URL", raising=False)
    monkeypatch.delenv("LANGGRAPH_STRICT_MSGPACK", raising=False)

    repo = tmp_path / "repository"
    repo.mkdir()
    _git_init(repo)
    (tmp_path / "architecture.md").write_text(
        "# Architecture\n\n- INV-CONFIG-001 one pinned execution-policy snapshot per run.\n",
        encoding="utf-8",
    )
    config = tmp_path / "converge.yaml"
    _write_source_config(config, max_repairs=1, mode="cloud")

    # The source mapping is raw (un-normalized): relative paths + mode/profile_sets, no profiles.
    raw = yaml.safe_load(config.read_text(encoding="utf-8"))
    assert raw["project"]["repo_path"] == "repository"
    assert raw["models"]["mode"] == "cloud"
    assert set(raw["models"]) == {"mode", "profile_sets"}

    registry_path = tmp_path / "control.sqlite"
    controller = ScheduledRunController(registry_path, restore_on_start=False)
    controller.register_project("fixture", config)

    # Start the run: materialize the single hash-pinned snapshot and record its metadata.
    _, snapshot, digest = materialize_run_config_snapshot(config, "run-1")
    record = controller.registry.create_run(
        "run-1",
        "fixture",
        "run-1",
        config_snapshot_path=snapshot,
        config_snapshot_sha256=digest,
    )
    assert record["status"] == "queued"
    assert record["finished_at"] is None
    assert record["config_snapshot_path"] == str(snapshot)
    assert record["config_snapshot_sha256"] == digest

    # The source config drifts after the run has already started.
    _write_source_config(config, max_repairs=7, mode="local")
    drifted = yaml.safe_load(config.read_text(encoding="utf-8"))
    assert drifted["models"]["mode"] == "local"
    assert drifted["workflow"]["max_repair_attempts"] == 7

    # A freshly constructed controller (process restart) must still serve the pinned snapshot.
    fresh = ScheduledRunController(registry_path, restore_on_start=False)
    cfg = fresh._config_for_run(record)
    assert cfg.max_repair_attempts == 1
    assert cfg.model_profiles["default"].model == "cloud-profile"
    assert cfg.repo_path == (tmp_path / "repository").resolve()
    assert cfg.state_dir == (tmp_path / ".converge").resolve()
    assert fresh._config_path_for_run(record) == str(snapshot)
    assert load_run_config_snapshot(snapshot, digest).max_repair_attempts == 1

    # The snapshot content is normalized (absolute paths + resolved profiles), not the raw mapping.
    snap = yaml.safe_load(snapshot.read_text(encoding="utf-8"))
    assert snap["project"]["repo_path"] == str((tmp_path / "repository").resolve())
    assert snap["project"]["state_dir"] == str((tmp_path / ".converge").resolve())
    assert snap["models"]["profiles"]["default"]["model"] == "cloud-profile"
    assert "mode" not in snap["models"]
    assert "profile_sets" not in snap["models"]

    # Fail closed: a mismatched snapshot hash does not match the immutable snapshot path.
    with pytest.raises(RuntimeError, match="Pinned run configuration metadata does not match"):
        load_run_config_snapshot(snapshot, "0" * 64)

    # Fail closed: a tampered snapshot content refuses to serve the durable run.
    snapshot.write_text(snapshot.read_text(encoding="utf-8") + "# tampered\n", encoding="utf-8")
    with pytest.raises(RuntimeError, match="Pinned run configuration changed"):
        load_run_config_snapshot(snapshot, digest)
