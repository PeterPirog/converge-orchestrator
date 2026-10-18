from __future__ import annotations

import textwrap
from pathlib import Path
from typing import Any

import pytest

from converge_orchestrator.config import load_config

# Literal RED-lock marker: the pure resolver that enforces REQ-SOT-001 does not exist
# yet, so every attempt to resolve the single authoritative Source of Truth fails with
# this exact marker until converge_orchestrator.config.resolve_source_of_truth is added.
_MARKER = "SOT001_SINGLE_SOURCE_OF_TRUTH_MISSING"


def _sot_resolver(data: dict[str, Any]) -> Any:
    """Resolve the single authoritative Source of Truth declared by ``data``.

    REQ-SOT-001: a run SHALL accept exactly one authoritative Markdown Source of Truth.
    ``converge_orchestrator.config.resolve_source_of_truth`` is the pure resolver that
    enforces that contract. It does not exist yet, so this helper raises AssertionError
    with the literal marker ``SOT001_SINGLE_SOURCE_OF_TRUTH_MISSING`` (deterministic RED
    lock) instead of silently accepting a run with zero or multiple declarations.
    """
    try:
        from converge_orchestrator.config import resolve_source_of_truth
    except ImportError as exc:
        raise AssertionError(_MARKER) from exc
    return resolve_source_of_truth(data)


def _write_config(tmp_path: Path, body: str) -> Path:
    (tmp_path / "repository").mkdir(exist_ok=True)
    config_path = tmp_path / "converge.yaml"
    config_path.write_text(textwrap.dedent(body).strip() + "\n", encoding="utf-8")
    return config_path


def test_resolver_accepts_exactly_one_source_of_truth() -> None:
    # A single declaration, whether top-level or nested under ``project``, is the one
    # and only authoritative Source of Truth and resolves to its declared path.
    assert _sot_resolver({"requirements_path": "sot.md"}) == "sot.md"
    assert _sot_resolver({"project": {"requirements_path": "sot.md"}}) == "sot.md"


def test_resolver_rejects_a_configuration_without_a_source_of_truth() -> None:
    # Zero declarations: a run with no authoritative Source of Truth must fail closed.
    with pytest.raises(ValueError):
        _sot_resolver({"repo_path": "repository"})


def test_resolver_rejects_multiple_source_of_truth_declarations() -> None:
    # Two declarations (flat plus project.requirements_path) are ambiguous: more than
    # one authoritative Source of Truth is not acceptable, so it must fail closed.
    with pytest.raises(ValueError):
        _sot_resolver(
            {
                "requirements_path": "top.md",
                "project": {"requirements_path": "nested.md"},
            }
        )


def test_load_config_rejects_multiple_source_of_truth_declarations(tmp_path: Path) -> None:
    config_path = _write_config(
        tmp_path,
        """
        version: 1
        requirements_path: ./top.md
        project:
          repo_path: ./repository
          requirements_path: ./nested.md
          require_spec_read_only: false
        agents:
          planner:
            agent: converge-planner
            model: openai/example
        """,
    )
    with pytest.raises(ValueError):
        load_config(config_path)


def test_load_config_rejects_a_configuration_without_a_source_of_truth(
    tmp_path: Path,
) -> None:
    config_path = _write_config(
        tmp_path,
        """
        version: 1
        project:
          repo_path: ./repository
          require_spec_read_only: false
        agents:
          planner:
            agent: converge-planner
            model: openai/example
        """,
    )
    with pytest.raises(ValueError):
        load_config(config_path)
