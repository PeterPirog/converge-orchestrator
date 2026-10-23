from __future__ import annotations

from typing import Any

# Literal RED-lock marker: the pure guard that enforces REQ-SOT-002 does not exist
# yet, so every attempt to evaluate Source-of-Truth isolation fails with this exact
# marker until
# converge_orchestrator.config.evaluate_source_of_truth_isolation is added.
_MARKER = "SOT002_SOURCE_OF_TRUTH_PROTECTION_MISSING"

# Fixed, distinct roots keep the isolation comparison in the tests deterministic and
# unambiguous, while the common root below lets one Source-of-Truth path sit inside a
# candidate worktree and a model-role-writable root at the same time.
_SOT_PATH = "/data/requirements/CONVERGE_ORCHESTRATOR_2_0_SOURCE_OF_TRUTH.md"
_WORKTREE_ROOT = "/worktrees/feature-branch"
_WRITABLE_ROOT = "/models/writable"
_SHARED_ROOT = "/shared/root"


def _evaluate_isolation(state: dict[str, Any]) -> tuple[str, ...]:
    """Evaluate Source-of-Truth isolation for ``state``.

    REQ-SOT-002: the Source of Truth SHALL reside outside every writable candidate
    worktree and SHALL NOT be modifiable by any model-backed role.
    ``converge_orchestrator.config.evaluate_source_of_truth_isolation`` is the pure,
    offline guard that enforces that contract. It does not exist yet, so this helper
    raises AssertionError with the literal marker
    ``SOT002_SOURCE_OF_TRUTH_PROTECTION_MISSING`` (deterministic RED lock) instead of
    silently accepting a run whose Source of Truth sits inside a candidate worktree or
    a model-role-writable root.
    """
    try:
        from converge_orchestrator.config import evaluate_source_of_truth_isolation
    except ImportError as exc:
        raise AssertionError(_MARKER) from exc
    return evaluate_source_of_truth_isolation(state)


def test_source_of_truth_outside_worktrees_is_accepted() -> None:
    # The Source of Truth sits outside every candidate worktree and every
    # model-role-writable root: the guard is satisfied and reports no clause.
    state = {
        "source_of_truth_path": _SOT_PATH,
        "candidate_worktree_paths": [_WORKTREE_ROOT],
        "model_role_writable_paths": [_WRITABLE_ROOT],
    }
    assert _evaluate_isolation(state) == ()


def test_source_of_truth_inside_candidate_worktree_fails_closed() -> None:
    # The Source of Truth lives under a candidate worktree: the guard must fail closed
    # with exactly the candidate-worktree clause and nothing else.
    state = {
        "source_of_truth_path": f"{_WORKTREE_ROOT}/nested/SOURCE.md",
        "candidate_worktree_paths": [_WORKTREE_ROOT],
        "model_role_writable_paths": [_WRITABLE_ROOT],
    }
    assert _evaluate_isolation(state) == ("source_of_truth_inside_candidate_worktree",)


def test_source_of_truth_model_role_writable_fails_closed() -> None:
    # The Source of Truth lives under a model-role-writable root: the guard must fail
    # closed with exactly the model-role-writable clause and nothing else.
    state = {
        "source_of_truth_path": f"{_WRITABLE_ROOT}/nested/SOURCE.md",
        "candidate_worktree_paths": [_WORKTREE_ROOT],
        "model_role_writable_paths": [_WRITABLE_ROOT],
    }
    assert _evaluate_isolation(state) == ("source_of_truth_model_role_writable",)


def test_source_of_truth_in_both_fails_closed_in_fixed_order() -> None:
    # The Source of Truth sits inside a candidate worktree and a model-role-writable
    # root at once: both clauses must be reported in the fixed order (candidate
    # worktree first, then model-role-writable).
    state = {
        "source_of_truth_path": f"{_SHARED_ROOT}/nested/SOURCE.md",
        "candidate_worktree_paths": [_SHARED_ROOT],
        "model_role_writable_paths": [_SHARED_ROOT],
    }
    assert _evaluate_isolation(state) == (
        "source_of_truth_inside_candidate_worktree",
        "source_of_truth_model_role_writable",
    )


def test_missing_source_of_truth_path_is_deferred_to_req_sot_001() -> None:
    # No Source of Truth path is declared: presence/absence is REQ-SOT-001's concern
    # (enforced by resolve_source_of_truth), so this guard reports no clause rather
    # than inventing a new failure mode.
    state = {
        "candidate_worktree_paths": [_WORKTREE_ROOT],
        "model_role_writable_paths": [_WRITABLE_ROOT],
    }
    assert _evaluate_isolation(state) == ()
