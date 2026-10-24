"""REQ-CONFIG-007: a new run binds a normalized, secret-free configuration snapshot by digest.

REQ-CONFIG-007 (CONVERGE_ORCHESTRATOR_2_0_SOURCE_OF_TRUTH.md:L205
[6. Project configuration and secret sources]): a new run SHALL normalize the
effective non-secret configuration into a durable immutable snapshot, bind that
snapshot by digest to the run, and use that exact snapshot for subsequent
recovery.

These offline, deterministic regression tests lock the full new-run lifecycle:
``start_run`` materializes a normalized, secret-free snapshot under
``state_dir/run-configs`` and pins its SHA-256 digest in the durable run row, a
freshly constructed controller recovers that exact snapshot even after the
source ``converge.yaml`` has drifted, and every pinning invariant fails closed.
No network, model, provider, or external-service access occurs outside pytest
``tmp_path``.
"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path
from unittest.mock import Mock

import pytest
import yaml

from converge_orchestrator.config import (
    load_run_config_snapshot,
    materialize_run_config_snapshot,
)
from converge_orchestrator.runtime_service import ScheduledRunController

GATEWAY_KEY_ENV = "CONVERGE_REQ_CONFIG_007_GATEWAY_KEY"
GATEWAY_KEY_VALUE = "gateway-secret-value-req-config-007-a1b2c3d4"
_HEX64 = re.compile(r"[0-9a-f]{64}")


def _git_init(repo: Path) -> None:
    subprocess.run(["git", "init", "-b", "main", str(repo)], check=True, capture_output=True)


def _write_config(config: Path, *, max_repairs: int, mode: str) -> None:
    """Write the raw (un-normalized) source mapping: relative paths + mode/profile_sets."""
    config.parent.mkdir(parents=True, exist_ok=True)
    config.write_text(
        yaml.safe_dump(
            {
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
                    "gateway": {
                        "kind": "openwebui",
                        "base_url": "http://127.0.0.1:3000/api",
                        "api_key_env": GATEWAY_KEY_ENV,
                    },
                    "mode": mode,
                    "profile_sets": {
                        "cloud": {"default": {"model": "cloud-profile"}},
                        "local": {"default": {"model": "local-profile"}},
                    },
                },
                "workflow": {"max_repair_attempts": max_repairs},
            },
            sort_keys=True,
        ),
        encoding="utf-8",
    )


def _setup(tmp_path: Path, monkeypatch) -> Path:
    """Create a git repository, a Source of Truth, and a raw (un-normalized) converge.yaml."""
    # Keep the run fully offline/deterministic: never select the Postgres backend or
    # msgpack-strict mode from an ambient test-process environment.
    monkeypatch.delenv("CONVERGE_DATABASE_URL", raising=False)
    monkeypatch.delenv("LANGGRAPH_STRICT_MSGPACK", raising=False)
    # The secret value is available in the process environment; the snapshot must still
    # record only the environment-variable name, never the resolved value.
    monkeypatch.setenv(GATEWAY_KEY_ENV, GATEWAY_KEY_VALUE)
    repo = tmp_path / "repository"
    repo.mkdir()
    _git_init(repo)
    (tmp_path / "architecture.md").write_text(
        "# Architecture\n\n- REQ-CONFIG-007 one digest-bound snapshot per run.\n",
        encoding="utf-8",
    )
    config = tmp_path / "converge.yaml"
    _write_config(config, max_repairs=1, mode="cloud")
    return config


def test_start_run_binds_digest_pinned_snapshot_and_submits_it(tmp_path: Path, monkeypatch) -> None:
    """start_run materializes a normalized, secret-free snapshot and pins it by digest.

    The durable run row records the resolved snapshot path and SHA-256 digest, the
    snapshot is written at ``state_dir/run-configs/<run_id>-sha256-<64 hex>.yaml``, and
    the initial input submitted to the worker uses that exact snapshot as config_path.
    """

    config = _setup(tmp_path, monkeypatch)
    controller = ScheduledRunController(tmp_path / "control.sqlite", restore_on_start=False)
    controller.register_project("fixture", config)
    controller._submit = Mock()  # type: ignore[method-assign]

    record = controller.start_run("fixture")

    digest = record["config_snapshot_sha256"]
    assert record["status"] == "queued"
    assert record["finished_at"] is None
    assert _HEX64.fullmatch(digest)

    snapshot_name = f"{record['id']}-sha256-{digest}.yaml"
    # The durable row stores the resolved snapshot path (registry.py records it via
    # Path(...).resolve()), so compare against the canonical spelling of this fixture path.
    expected = (tmp_path / ".converge" / "run-configs" / snapshot_name).resolve()
    assert record["config_snapshot_path"] == str(expected)
    assert expected.is_file()

    # The worker is seeded with the pinned snapshot, not the mutable source YAML.
    run_id, initial = controller._submit.call_args.args
    assert run_id == record["id"]
    assert initial["config_path"] == str(expected)
    assert initial["run_id"] == record["id"]
    assert initial["project_id"] == "fixture"

    # The snapshot content is normalized: absolute paths, selected profile, no mode/sets.
    snap = yaml.safe_load(expected.read_text(encoding="utf-8"))
    assert snap["project"]["repo_path"] == str((tmp_path / "repository").resolve())
    assert snap["project"]["state_dir"] == str((tmp_path / ".converge").resolve())
    assert snap["models"]["profiles"]["default"]["model"] == "cloud-profile"
    assert "mode" not in snap["models"]
    assert "profile_sets" not in snap["models"]

    # The snapshot stores the env-var reference, never the resolved secret value.
    content = expected.read_text(encoding="utf-8")
    assert GATEWAY_KEY_VALUE not in content
    assert f"api_key_env: {GATEWAY_KEY_ENV}" in content

    reloaded = load_run_config_snapshot(expected, digest)
    assert reloaded.model_profiles["default"].model == "cloud-profile"
    assert reloaded.model_gateway.api_key_env == GATEWAY_KEY_ENV


def test_fresh_controller_recovers_pinned_snapshot_after_source_drift(
    tmp_path: Path,
    monkeypatch,
) -> None:
    """A restart serves the pinned snapshot even after the source converge.yaml drifts.

    A freshly constructed controller (process restart) must recover the original run
    values from the pinned snapshot -- not the drifted source -- through
    ``_config_for_run``, ``_config_path_for_run``, and ``_initial_recovery_input``.
    """

    config = _setup(tmp_path, monkeypatch)
    registry_path = tmp_path / "control.sqlite"
    controller = ScheduledRunController(registry_path, restore_on_start=False)
    controller.register_project("fixture", config)
    controller._submit = Mock()  # type: ignore[method-assign]
    record = controller.start_run("fixture")
    snapshot_path = record["config_snapshot_path"]
    digest = record["config_snapshot_sha256"]

    # The source config drifts after the run has already started.
    drifted = yaml.safe_load(config.read_text(encoding="utf-8"))
    drifted["workflow"]["max_repair_attempts"] = 9
    drifted["models"]["mode"] = "local"
    config.write_text(yaml.safe_dump(drifted, sort_keys=True), encoding="utf-8")
    assert yaml.safe_load(config.read_text(encoding="utf-8"))["models"]["mode"] == "local"

    # A freshly constructed controller (process restart) serves the pinned snapshot.
    fresh = ScheduledRunController(registry_path, restore_on_start=False)
    cfg = fresh._config_for_run(record)
    assert cfg.max_repair_attempts == 1
    assert cfg.model_profiles["default"].model == "cloud-profile"
    assert fresh._config_path_for_run(record) == snapshot_path
    assert fresh._initial_recovery_input(record) == {
        "project_id": record["project_id"],
        "config_path": snapshot_path,
        "run_id": record["id"],
        "thread_id": record["thread_id"],
    }
    assert load_run_config_snapshot(snapshot_path, digest).max_repair_attempts == 1


def test_run_config_snapshot_fails_closed(tmp_path: Path, monkeypatch) -> None:
    """Every pinning invariant fails closed rather than falling back to the source YAML.

    Re-materializing the same run_id refuses to overwrite the immutable snapshot, a
    mismatched expected digest raises, and a tampered snapshot raises
    ``Pinned run configuration changed`` instead of silently serving drifted content.
    """

    config = _setup(tmp_path, monkeypatch)
    _, snapshot, digest = materialize_run_config_snapshot(config, "run-1")

    # Re-materializing the same run_id refuses to overwrite the immutable snapshot.
    with pytest.raises(RuntimeError, match="Run configuration snapshot already exists"):
        materialize_run_config_snapshot(config, "run-1")

    # A mismatched expected digest does not match the immutable snapshot path.
    with pytest.raises(RuntimeError, match="Pinned run configuration metadata does not match"):
        load_run_config_snapshot(snapshot, "0" * 64)

    # A tampered snapshot fails closed instead of falling back to the current project YAML.
    snapshot.write_text(snapshot.read_text(encoding="utf-8") + "# tampered\n", encoding="utf-8")
    with pytest.raises(RuntimeError, match="Pinned run configuration changed"):
        load_run_config_snapshot(snapshot, digest)
