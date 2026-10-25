"""REQ-CONFIG-008: a broken pinned configuration snapshot SHALL fail closed.

REQ-CONFIG-008 (CONVERGE_ORCHESTRATOR_2_0_SOURCE_OF_TRUTH.md:L206
[6. Project configuration and secret sources]): a missing, incomplete,
incompatible, or digest-mismatched pinned configuration snapshot SHALL fail
closed rather than falling back to the current mutable project configuration.

These offline, deterministic regression tests lock the run-recovery path for a
run that carries pinned configuration metadata (``RunController._config_for_run``):
each malformed pinned-snapshot condition -- the snapshot file is missing, the
pinned metadata is incomplete, the snapshot content is not a valid project
configuration, or the snapshot digest no longer matches -- raises instead of
consulting the mutable per-project configuration
(``RunController._config_for_project``). The legacy, unpinned run row retains its
historical fallback so the fail-closed behavior does not weaken it. No network,
model, provider, or external-service access occurs outside pytest ``tmp_path``.
"""

from __future__ import annotations

import hashlib
from pathlib import Path
from unittest.mock import Mock, patch

import pytest
import yaml

from converge_orchestrator.config import materialize_run_config_snapshot
from converge_orchestrator.runtime import RunController

_RUN_ID = "run-1"
_PROJECT_ID = "fixture"


def _write_config(tmp_path: Path, *, max_repairs: int = 1) -> Path:
    repo = tmp_path / "repository"
    repo.mkdir(exist_ok=True)
    requirements = tmp_path / "architecture.md"
    requirements.write_text(
        "# Architecture\n\n- REQ-CONFIG-008 a broken pinned snapshot fails closed.\n",
        encoding="utf-8",
    )
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


def _controller() -> tuple[RunController, Mock]:
    """Build a controller whose mutable project-config fallback is spied on.

    Returns the controller together with the ``Mock`` that stands in for
    ``_config_for_project`` so tests can assert whether the mutable fallback was
    consulted.
    """
    controller = object.__new__(RunController)
    controller.registry = Mock()
    controller.registry.get_project.return_value = {
        "id": _PROJECT_ID,
        "config_path": "/projects/fixture/converge.yaml",
        "workspace_id": "workspace",
        "state_store_id": "state-store",
    }
    # Spy on the mutable per-project configuration fallback: on any
    # pinned-snapshot failure path it must never be consulted.
    spy = Mock()
    controller._config_for_project = spy  # type: ignore[method-assign]
    return controller, spy


def _record(snapshot_path: str | None, snapshot_sha256: str | None) -> dict:
    return {
        "id": _RUN_ID,
        "project_id": _PROJECT_ID,
        "config_snapshot_path": snapshot_path,
        "config_snapshot_sha256": snapshot_sha256,
    }


def test_missing_pinned_snapshot_fails_closed(tmp_path: Path) -> None:
    """A missing pinned snapshot raises instead of falling back to the project config."""
    controller, spy = _controller()
    run_configs = tmp_path / ".converge" / "run-configs"
    run_configs.mkdir(parents=True)
    digest = hashlib.sha256(b"missing-snapshot-content").hexdigest()
    snapshot_path = run_configs / f"{_RUN_ID}-sha256-{digest}.yaml"
    assert not snapshot_path.exists()

    record = _record(str(snapshot_path), digest)

    with pytest.raises(FileNotFoundError):
        controller._config_for_run(record)

    spy.assert_not_called()


def test_incomplete_pinned_metadata_fails_closed() -> None:
    """A pinned path without a digest, or a digest without a path, fails closed."""
    controller, spy = _controller()

    # Snapshot path present but the digest is missing.
    record = _record("/state/run-configs/run-1-sha256-aaaa.yaml", None)
    with pytest.raises(RuntimeError, match="incomplete pinned configuration metadata"):
        controller._config_for_run(record)

    # Digest present but the snapshot path is missing.
    record = _record(None, "a" * 64)
    with pytest.raises(RuntimeError, match="incomplete pinned configuration metadata"):
        controller._config_for_run(record)

    spy.assert_not_called()


def test_incompatible_snapshot_content_fails_closed(tmp_path: Path) -> None:
    """A pinned snapshot whose content is not a valid project config fails closed."""
    controller, spy = _controller()
    run_configs = tmp_path / ".converge" / "run-configs"
    run_configs.mkdir(parents=True)
    # The digest in the path matches the content, but the content is not a valid config.
    bad_content = "- unstructured\n- snapshot\n"
    bad_digest = hashlib.sha256(bad_content.encode("utf-8")).hexdigest()
    bad_snapshot = run_configs / f"{_RUN_ID}-sha256-{bad_digest}.yaml"
    bad_snapshot.write_text(bad_content, encoding="utf-8", newline="\n")

    record = _record(str(bad_snapshot), bad_digest)

    with pytest.raises(ValueError, match="must contain a YAML mapping"):
        controller._config_for_run(record)

    spy.assert_not_called()


def test_digest_mismatched_snapshot_content_fails_closed(tmp_path: Path) -> None:
    """A tampered pinned snapshot whose content digest no longer matches fails closed."""
    controller, spy = _controller()
    source = _write_config(tmp_path)
    _, snapshot, digest = materialize_run_config_snapshot(source, _RUN_ID)
    # Tamper with the pinned content so its digest no longer matches the path.
    snapshot.write_text(
        snapshot.read_text(encoding="utf-8") + "# tampered\n",
        encoding="utf-8",
        newline="\n",
    )

    record = _record(str(snapshot), digest)

    with pytest.raises(RuntimeError, match="Pinned run configuration changed"):
        controller._config_for_run(record)

    spy.assert_not_called()


def test_digest_mismatched_pinned_metadata_fails_closed(tmp_path: Path) -> None:
    """A run row whose pinned digest does not match the snapshot path fails closed."""
    controller, spy = _controller()
    source = _write_config(tmp_path)
    _, snapshot, _ = materialize_run_config_snapshot(source, _RUN_ID)

    # The recorded digest does not match the immutable digest in the snapshot path.
    record = _record(str(snapshot), "0" * 64)

    with pytest.raises(
        RuntimeError, match="Pinned run configuration metadata does not match"
    ):
        controller._config_for_run(record)

    spy.assert_not_called()


def test_valid_pinned_snapshot_is_served_without_fallback(tmp_path: Path) -> None:
    """A well-formed pinned snapshot is served and the project config is not consulted."""
    controller, spy = _controller()
    source = _write_config(tmp_path)
    _, snapshot, digest = materialize_run_config_snapshot(source, _RUN_ID)

    record = _record(str(snapshot), digest)

    with (
        patch("converge_orchestrator.runtime.assert_workspace_affinity"),
        patch("converge_orchestrator.runtime.assert_state_store_affinity"),
    ):
        cfg = controller._config_for_run(record)

    assert cfg.max_repair_attempts == 1
    spy.assert_not_called()


def test_legacy_unpinned_run_still_consults_project_config() -> None:
    """A run with no pinned metadata retains its historical project-config fallback."""
    controller, spy = _controller()
    spy.return_value = "LEGACY-CONFIG"

    record = _record(None, None)

    cfg = controller._config_for_run(record)

    assert cfg == "LEGACY-CONFIG"
    spy.assert_called_once()
