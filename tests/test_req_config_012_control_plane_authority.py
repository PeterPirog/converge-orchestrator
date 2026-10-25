"""REQ-CONFIG-012: control-plane authority stays outside candidate-writer mutation.

REQ-CONFIG-012 (CONVERGE_ORCHESTRATOR_2_0_SOURCE_OF_TRUTH.md:L210
[6. Project configuration and secret sources]): the authoritative
project-configuration source, pinned run snapshots, Source of Truth, secret
sources, and durable control state SHALL remain outside candidate-writer mutation
authority; any repository-tracked copy of control-plane configuration SHALL be
non-authoritative unless a later run explicitly adopts it through trusted project
configuration.

These offline, deterministic tests pin that contract against the pure, fail-closed
guard ``converge_orchestrator.config.evaluate_control_plane_authority``:

* the guard returns an empty tuple when every control-plane artifact sits outside
  all candidate-writer roots and no unadopted repository-tracked copy is treated
  as authoritative;
* the guard reports the corresponding isolation clause for project configuration,
  pinned run snapshots, Source of Truth, secret sources, and durable control state
  in fixed artifact order when any such path sits inside a candidate-writer root;
* the guard reports
  ``repository_tracked_control_plane_copy_is_authoritative`` when a
  repository-tracked copy is treated as authoritative without explicit adoption,
  and accepts the same copy once it is explicitly adopted through trusted project
  configuration;
* the guard fails closed with ``ValueError`` on a missing control-plane path
  declaration, missing candidate-writer roots, or a malformed or relative path.

The guard does not exist yet, so every evaluation below fails with the literal
TDD RED evidence marker ``REQ-CONFIG-012-CONTROL-PLANE-AUTHORITY-GUARD-MISSING``
instead of silently accepting a run whose control-plane authority has leaked into a
candidate-writer mutation root. No network, model, git, subprocess, or filesystem
access occurs.
"""

from __future__ import annotations

from typing import Any

import pytest

# TDD RED evidence marker (tdd.expected_failure_pattern): carried by every failure
# below until the pure guard converge_orchestrator.config.evaluate_control_plane_authority
# is added. It is a literal, unique string that does not appear anywhere else in the
# repository, so its appearance in a test run is deterministic RED evidence.
_GUARD_MISSING = "REQ-CONFIG-012-CONTROL-PLANE-AUTHORITY-GUARD-MISSING"

# Fixed, distinct absolute roots keep the isolation comparison deterministic: every
# control-plane artifact lives under _CONTROL_ROOT while the single candidate-writer
# mutation root is _CANDIDATE_ROOT, so no artifact is inside the candidate-writer root
# by default and each "inside" test can move exactly one artifact in.
_CONTROL_ROOT = "/control"
_CANDIDATE_ROOT = "/candidates"
# A fixed, distinct 64-hex-char SHA-256 digest keeps the pinned run-snapshot path
# realistic and unambiguous without any filesystem access.
_DIGEST = "0123456789abcdef" * 4

# The five control-plane artifacts in the fixed order the requirement names them
# (project configuration, pinned run snapshots, Source of Truth, secret sources,
# durable control state), each mapped to its run-state key and isolation clause.
_ARTIFACTS = (
    ("project_config_path", "project_config_inside_candidate_writer_root"),
    ("pinned_run_snapshot_path", "pinned_run_snapshot_inside_candidate_writer_root"),
    ("source_of_truth_path", "source_of_truth_inside_candidate_writer_root"),
    ("secret_sources", "secret_source_inside_candidate_writer_root"),
    ("durable_control_state_path", "durable_control_state_inside_candidate_writer_root"),
)
# The repository-tracked-copy adoption clause is named verbatim by the requirement.
_ADOPTION_CLAUSE = "repository_tracked_control_plane_copy_is_authoritative"


def _guard():
    """Return the guard, failing closed with the literal RED marker when it is absent."""
    try:
        from converge_orchestrator.config import evaluate_control_plane_authority
    except ImportError as exc:
        raise AssertionError(_GUARD_MISSING) from exc
    return evaluate_control_plane_authority


def _evaluate(state: dict[str, Any]) -> tuple[str, ...]:
    """Evaluate control-plane authority for ``state`` through the guard."""
    return _guard()(state)


def _base_state() -> dict[str, Any]:
    """One run-state snapshot with every control-plane artifact outside the candidate root."""
    return {
        "project_config_path": f"{_CONTROL_ROOT}/project-config/converge.yaml",
        "pinned_run_snapshot_path": f"{_CONTROL_ROOT}/run-configs/run-001-sha256-{_DIGEST}.yaml",
        "source_of_truth_path": f"{_CONTROL_ROOT}/requirements/SOURCE_OF_TRUTH.md",
        "secret_sources": [f"{_CONTROL_ROOT}/secrets/.env"],
        "durable_control_state_path": f"{_CONTROL_ROOT}/state/control.sqlite",
        "candidate_writer_roots": [_CANDIDATE_ROOT],
    }


def _state_with_artifact_inside(state: dict[str, Any], key: str) -> dict[str, Any]:
    """Return ``state`` with exactly the artifact named by ``key`` moved into the candidate root."""
    moved = dict(state)
    if key == "secret_sources":
        moved[key] = [f"{_CANDIDATE_ROOT}/secrets/.env"]
    else:
        moved[key] = f"{_CANDIDATE_ROOT}/{key}"
    return moved


def test_all_artifacts_outside_and_no_unadopted_copy_is_accepted() -> None:
    # Every control-plane artifact sits outside the candidate-writer mutation root and
    # no repository-tracked copy is declared: the guard is satisfied and reports no clause.
    assert _evaluate(_base_state()) == ()


@pytest.mark.parametrize(
    "key,clause",
    _ARTIFACTS,
    ids=[clause for _, clause in _ARTIFACTS],
)
def test_artifact_inside_candidate_writer_root_reports_clause(key: str, clause: str) -> None:
    # Exactly one control-plane artifact sits inside the candidate-writer mutation root:
    # the guard must fail closed with exactly that artifact's isolation clause and nothing else.
    state = _state_with_artifact_inside(_base_state(), key)
    assert _evaluate(state) == (clause,)


def test_all_artifacts_inside_report_clauses_in_fixed_artifact_order() -> None:
    # Every control-plane artifact sits inside the candidate-writer mutation root at once:
    # the guard must report all five isolation clauses in the fixed artifact order named by
    # the requirement (project configuration, pinned run snapshots, Source of Truth, secret
    # sources, durable control state).
    state = dict(_base_state())
    for key, _ in _ARTIFACTS:
        state.update(_state_with_artifact_inside(state, key))
    assert _evaluate(state) == tuple(clause for _, clause in _ARTIFACTS)


def test_repo_tracked_copy_without_explicit_adoption_is_authoritative_violation() -> None:
    # A repository-tracked control-plane configuration copy is treated as authoritative
    # without explicit adoption: the guard must report the adoption clause.
    state = dict(_base_state())
    state["repository_tracked_control_plane_copy"] = {
        "path": "/repository/converge.yaml",
        "adopted": False,
    }
    assert _evaluate(state) == (_ADOPTION_CLAUSE,)


def test_repo_tracked_copy_explicitly_adopted_through_trusted_config_is_accepted() -> None:
    # The same repository-tracked copy is explicitly adopted through trusted project
    # configuration: the guard accepts it and reports no clause.
    state = dict(_base_state())
    state["repository_tracked_control_plane_copy"] = {
        "path": "/repository/converge.yaml",
        "adopted": True,
    }
    assert _evaluate(state) == ()


def test_missing_control_plane_path_declaration_fails_closed() -> None:
    # A control-plane path declaration is missing, so the guard cannot verify isolation:
    # it must fail closed with ValueError rather than silently passing.
    state = dict(_base_state())
    del state["source_of_truth_path"]
    with pytest.raises(ValueError):
        _evaluate(state)


def test_missing_candidate_writer_roots_fails_closed() -> None:
    # No candidate-writer mutation roots are declared, so isolation cannot be verified:
    # the guard must fail closed with ValueError rather than silently passing.
    state = dict(_base_state())
    del state["candidate_writer_roots"]
    with pytest.raises(ValueError):
        _evaluate(state)


def test_relative_control_plane_path_fails_closed() -> None:
    # A control-plane path is relative (not a plain absolute path), so the guard cannot
    # compare it against candidate-writer roots: it must fail closed with ValueError.
    state = dict(_base_state())
    state["project_config_path"] = "project-config/converge.yaml"
    with pytest.raises(ValueError):
        _evaluate(state)
