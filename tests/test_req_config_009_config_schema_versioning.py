"""REQ-CONFIG-009: configuration schema evolution is explicit; documented version 1 stays readable.

REQ-CONFIG-009 (CONVERGE_ORCHESTRATOR_2_0_SOURCE_OF_TRUTH.md:L207
[6. Project configuration and secret sources]): configuration schema evolution
SHALL use explicit versioning and deterministic migration or explicit rejection
rather than silent reinterpretation, and documented version-1.0 configuration
SHALL remain readable or receive a semantics-preserving migration path.

docs/CONFIGURATION.md (Top-level) states the contract these tests lock: ``version``
must currently be ``1``, and the earlier flat format remains accepted for backward
compatibility while new projects use the sectioned layout.

These offline, deterministic regression tests pin that contract at the public
``converge_orchestrator.config`` entry points:

* a documented sectioned version-1 configuration loads via ``load_config`` with
  ``version == 1`` and its declared fields intact;
* the same version-1 configuration materializes through
  ``materialize_run_config_snapshot`` and reloads through
  ``load_run_config_snapshot`` with unchanged version and semantics;
* an unsupported version (``2``) is explicitly rejected by ``load_config`` with a
  ``ValueError`` that names the field and the accepted value -- not silently
  reinterpreted as version 1; and
* a legacy flat version-1 document and a documented sectioned version-1 document
  carrying equivalent values normalize to identical observable ``ProjectConfig``
  fields, locking the semantics-preserving migration path out of the flat layout.

No network, model, provider, subprocess, or git access occurs outside pytest
``tmp_path``.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml
from pydantic import ValidationError

from converge_orchestrator.config import (
    load_config,
    load_run_config_snapshot,
    materialize_run_config_snapshot,
)

_RUN_ID = "run-1"


def _ensure_project(directory: Path) -> None:
    """Create the shared target repository and Source-of-Truth under ``directory``."""
    (directory / "repository").mkdir(exist_ok=True)
    (directory / "architecture.md").write_text(
        "# Architecture\n\n- REQ-CONFIG-009 version 1 configuration stays readable.\n",
        encoding="utf-8",
    )


def _sectioned_payload() -> dict:
    """Documented sectioned version-1 layout (docs/CONFIGURATION.md Top-level)."""
    return {
        "version": 1,
        "project": {
            "name": "payments",
            "repo_path": "repository",
            "requirements_path": "architecture.md",
            "state_dir": ".state",
            "worktree_dir": ".state/worktrees",
            "require_spec_read_only": False,
        },
        "github": {
            "repo": "acme/payments",
            "base_branch": "develop",
            "branch_prefix": "candidates/",
        },
        "opencode": {
            "binary": "opencode-custom",
            "auto_approve": False,
        },
        "models": {
            "profiles": {
                "planner": {"model": "anthropic/example"},
            },
        },
        "agents": {
            "planner": {"agent": "converge-planner", "model_profile": "planner"},
        },
        "quality": {"auto_discover": False},
        "workflow": {"max_repair_attempts": 5},
    }


def _flat_payload() -> dict:
    """Legacy flat version-1 layout carrying values equivalent to :func:`_sectioned_payload`."""
    return {
        "version": 1,
        "project_name": "payments",
        "repo_path": "repository",
        "requirements_path": "architecture.md",
        "state_dir": ".state",
        "worktree_dir": ".state/worktrees",
        "require_spec_read_only": False,
        "github_repo": "acme/payments",
        "base_branch": "develop",
        "branch_prefix": "candidates/",
        "opencode_binary": "opencode-custom",
        "opencode_auto_approve": False,
        "model_profiles": {
            "planner": {"model": "anthropic/example"},
        },
        "agents": {
            "planner": {"agent": "converge-planner", "model_profile": "planner"},
        },
        "auto_discover_quality": False,
        "max_repair_attempts": 5,
    }


def _write_config(directory: Path, name: str, payload: dict) -> Path:
    """Write a converge-style YAML document under ``directory`` and return its path."""
    config_path = directory / name
    config_path.write_text(yaml.safe_dump(payload, sort_keys=True), encoding="utf-8")
    return config_path


def test_documented_sectioned_version_1_config_loads_intact(tmp_path: Path) -> None:
    """A documented sectioned version-1 config loads with version == 1 and fields intact."""
    _ensure_project(tmp_path)
    config_path = _write_config(tmp_path, "converge.yaml", _sectioned_payload())

    cfg = load_config(config_path)

    assert cfg.version == 1
    assert cfg.project_name == "payments"
    assert cfg.repo_path == (tmp_path / "repository").resolve()
    assert cfg.requirements_path == (tmp_path / "architecture.md").resolve()
    assert cfg.state_dir == (tmp_path / ".state").resolve()
    assert cfg.worktree_dir == (tmp_path / ".state" / "worktrees").resolve()
    assert cfg.github_repo == "acme/payments"
    assert cfg.base_branch == "develop"
    # Non-default values pin the flattening path: each declared section field must
    # reach its documented top-level field rather than being dropped or left at the
    # ProjectConfig default (which would mask a silently-ignored section).
    assert cfg.branch_prefix == "candidates/"
    assert cfg.opencode_binary == "opencode-custom"
    assert cfg.opencode_auto_approve is False
    assert cfg.require_spec_read_only is False
    assert cfg.auto_discover_quality is False
    assert cfg.model_profiles["planner"].model == "anthropic/example"
    assert cfg.agents["planner"].agent == "converge-planner"
    assert cfg.agents["planner"].model_profile == "planner"
    assert cfg.max_repair_attempts == 5


def test_version_1_config_round_trips_through_pinned_snapshot(tmp_path: Path) -> None:
    """A version-1 config materializes to a pinned snapshot and reloads unchanged."""
    _ensure_project(tmp_path)
    config_path = _write_config(tmp_path, "converge.yaml", _sectioned_payload())

    original = load_config(config_path)

    cfg, snapshot, digest = materialize_run_config_snapshot(config_path, _RUN_ID)
    assert cfg.version == 1
    assert snapshot.name == f"{_RUN_ID}-sha256-{digest}.yaml"
    assert snapshot.is_file()

    reloaded = load_run_config_snapshot(snapshot, digest)
    assert reloaded.version == 1
    # Unchanged version and semantics: the pinned snapshot is the same configuration.
    assert reloaded.model_dump() == original.model_dump()


def test_unsupported_version_2_is_explicitly_rejected(tmp_path: Path) -> None:
    """A version-2 config is rejected by name, not silently reinterpreted as version 1."""
    _ensure_project(tmp_path)
    payload = _sectioned_payload()
    payload["version"] = 2
    config_path = _write_config(tmp_path, "converge.yaml", payload)

    with pytest.raises(ValueError) as excinfo:
        load_config(config_path)

    error = excinfo.value
    message = str(error).lower()
    # Explicit rejection rather than silent coercion to version 1: the failure is
    # localized to the version field and names both the accepted value (1) and the
    # rejected value (2). These checks are semantic, not coupled to validator prose.
    assert "version" in message

    if isinstance(error, ValidationError):
        version_errors = [
            entry for entry in error.errors() if tuple(entry.get("loc", ())) == ("version",)
        ]
        assert version_errors, f"expected a 'version' error location; message: {message}"
        entry = version_errors[0]
        assert entry.get("input") == 2, "the rejected version value (2) must be named"
        assert "1" in str(entry.get("msg")), "the accepted version value (1) must be named"
    else:  # Defensive: a non-pydantic validator must still name both values in prose.
        assert "2" in message, "the rejected version value (2) must be named"
        assert "1" in message, "the accepted version value (1) must be named"
    # Validation precedes effects: a rejected config creates no state directory.
    assert not (tmp_path / ".state").exists()


def test_flat_and_sectioned_equivalent_configs_yield_identical_fields(
    tmp_path: Path,
) -> None:
    """Flat and sectioned version-1 documents with equivalent values normalize identically."""
    _ensure_project(tmp_path)
    sectioned_path = _write_config(tmp_path, "converge.sectioned.yaml", _sectioned_payload())
    flat_path = _write_config(tmp_path, "converge.flat.yaml", _flat_payload())

    sectioned = load_config(sectioned_path)
    flat = load_config(flat_path)

    assert sectioned.version == 1
    assert flat.version == 1
    # The semantics-preserving migration path: equivalent values yield identical fields.
    assert flat.model_dump() == sectioned.model_dump()
    assert flat.project_name == sectioned.project_name == "payments"
    assert flat.github_repo == sectioned.github_repo == "acme/payments"
    assert flat.base_branch == sectioned.base_branch == "develop"
    assert flat.opencode_binary == sectioned.opencode_binary == "opencode-custom"
    assert flat.model_profiles == sectioned.model_profiles
    assert flat.agents == sectioned.agents
    assert flat.max_repair_attempts == sectioned.max_repair_attempts == 5
