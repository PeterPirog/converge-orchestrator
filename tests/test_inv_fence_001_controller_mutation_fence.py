"""RED lock test for INV-FENCE-001: controller mutation-fence guard.

INV-FENCE-001 (CONVERGE_ORCHESTRATOR_2_0_SOURCE_OF_TRUTH.md:L177 [4. Global invariants]):
A controller that no longer owns the current run or project mutation fencing epoch SHALL NOT
perform repository, remote, compliance, or other externally visible mutating side effects for
that authority.

The codebase has no controller mutation-fencing primitive. These deterministic offline tests
therefore lock the not-yet-existing pure primitive ``converge_orchestrator.fencing.
evaluate_mutation_fence``:

- against the old implementation the module/symbol does not exist, so every test fails with the
  literal marker ``FENCE001_CONTROLLER_MUTATION_FENCE_GUARD_MISSING``;
- once the primitive is added, the same tests must pass. It takes a plain mapping describing the
  controller's claimed mutation fencing epochs and the authoritative current epochs and returns
  the lost-fence clauses in a fixed order:

  * ``"run_fence_lost"``     -- the controller's claimed run mutation fencing epoch no longer
    matches the authoritative current run epoch (it is stale, absent, or mismatched);
  * ``"project_fence_lost"`` -- the controller's claimed project mutation fencing epoch no longer
    matches the authoritative current project epoch (it is stale, absent, or mismatched).

A controller whose claimed run and project mutation fencing epochs both match the authoritative
current epochs yields an empty result. All inputs are plain values so the predicate stays pure and
deterministic in memory. No network, model, provider, subprocess, git, or filesystem side effects
in the tested path.
"""

from __future__ import annotations

import importlib
from collections.abc import Callable, Iterable
from typing import cast

_MISSING = "FENCE001_CONTROLLER_MUTATION_FENCE_GUARD_MISSING"
_MODULE = "converge_orchestrator.fencing"
_NAME = "evaluate_mutation_fence"

_CLAUSE_RUN_FENCE_LOST = "run_fence_lost"
_CLAUSE_PROJECT_FENCE_LOST = "project_fence_lost"

# A pure guard: a plain mutation-fence mapping -> the (possibly empty) iterable of lost-fence
# clauses, in the fixed order run fence first, then project fence.
Guard = Callable[[object], Iterable[str]]


def _guard() -> Guard:
    """Resolve the INV-FENCE-001 primitive, failing with the RED marker if it is absent.

    Every test routes through this helper first, so against the old tree (where
    ``converge_orchestrator.fencing`` does not exist) each test fails with the literal
    ``FENCE001_CONTROLLER_MUTATION_FENCE_GUARD_MISSING`` marker rather than an opaque import
    error.
    """
    try:
        module = importlib.import_module(_MODULE)
    except ModuleNotFoundError as exc:
        raise AssertionError(
            f"{_MISSING}: {_MODULE}.{_NAME} is not defined; "
            "INV-FENCE-001 requires a deterministic, offline controller mutation-fence guard that "
            "fails closed whenever the controller's claimed run or project mutation fencing epoch "
            "no longer matches the authoritative current epoch."
        ) from exc
    guard = getattr(module, _NAME, None)
    assert callable(guard), (
        f"{_MISSING}: {_MODULE}.{_NAME} is not defined; "
        "INV-FENCE-001 requires a deterministic controller mutation-fence guard primitive."
    )
    return cast(Guard, guard)


def _state(
    *,
    claimed_run: object = 7,
    claimed_project: object = 42,
    current_run: object = 7,
    current_project: object = 42,
) -> dict[str, object]:
    """Build a plain mutation-fence mapping with no coupling to any concrete controller state."""
    return {
        "claimed_run_fence_epoch": claimed_run,
        "claimed_project_fence_epoch": claimed_project,
        "current_run_fence_epoch": current_run,
        "current_project_fence_epoch": current_project,
    }


def test_matching_epochs_report_no_lost_fence() -> None:
    guard = _guard()
    # Both the run and project mutation fencing epochs the controller claims match the
    # authoritative current epochs, so no lost-fence clause is reported.
    assert tuple(guard(_state())) == ()


def test_stale_run_epoch_reports_run_fence_lost() -> None:
    guard = _guard()
    # The controller claims a run epoch older than the authoritative current one: it no longer
    # owns the current run fence, so the run clause is reported (the project fence still holds).
    assert tuple(guard(_state(claimed_run=5, current_run=10))) == (_CLAUSE_RUN_FENCE_LOST,)


def test_absent_run_epoch_reports_run_fence_lost() -> None:
    guard = _guard()
    # An absent (None) claimed run epoch cannot prove current-run ownership, so the guard fails
    # closed and reports the run clause.
    assert tuple(guard(_state(claimed_run=None, current_run=10))) == (_CLAUSE_RUN_FENCE_LOST,)


def test_mismatched_run_epoch_reports_run_fence_lost() -> None:
    guard = _guard()
    # The claimed run epoch differs from the authoritative current one in the other direction,
    # so the run clause is still reported.
    assert tuple(guard(_state(claimed_run=9, current_run=8))) == (_CLAUSE_RUN_FENCE_LOST,)


def test_stale_project_epoch_reports_project_fence_lost() -> None:
    guard = _guard()
    # The controller claims a project epoch older than the authoritative current one: the
    # project clause is reported (the run fence still holds).
    assert tuple(guard(_state(claimed_project=40, current_project=42))) == (
        _CLAUSE_PROJECT_FENCE_LOST,
    )


def test_absent_project_epoch_reports_project_fence_lost() -> None:
    guard = _guard()
    # An absent (None) claimed project epoch cannot prove current-project ownership, so the
    # guard fails closed and reports the project clause.
    assert tuple(guard(_state(claimed_project=None, current_project=42))) == (
        _CLAUSE_PROJECT_FENCE_LOST,
    )


def test_mismatched_project_epoch_reports_project_fence_lost() -> None:
    guard = _guard()
    # The claimed project epoch differs from the authoritative current one, so the project
    # clause is reported.
    assert tuple(guard(_state(claimed_project=43, current_project=42))) == (
        _CLAUSE_PROJECT_FENCE_LOST,
    )


def test_both_lost_epochs_report_clauses_in_fixed_order() -> None:
    guard = _guard()
    # Both the run and project mutation fencing epochs are lost; the clauses are reported in
    # the fixed order: run fence first, then project fence.
    assert tuple(
        guard(_state(claimed_run=5, current_run=10, claimed_project=40, current_project=42))
    ) == (_CLAUSE_RUN_FENCE_LOST, _CLAUSE_PROJECT_FENCE_LOST)
