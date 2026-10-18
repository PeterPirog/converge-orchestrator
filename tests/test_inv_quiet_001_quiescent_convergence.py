"""RED lock test for INV-QUIET-001: a converged status is a quiescent convergence state.

INV-QUIET-001 (CONVERGE_ORCHESTRATOR_2_0_SOURCE_OF_TRUTH.md:L178 [4. Global invariants]):
A terminal converged status SHALL represent a quiescent convergence state rather than a
momentary set of PASS records while mutation, waiting, drift reconciliation, or side-effect
outcome remains unresolved.

The codebase has no terminal-quiescence primitive. These deterministic offline tests therefore
lock the not-yet-existing pure predicate ``evaluate_terminal_quiescence`` on the module
``converge_orchestrator.quiescence``:

- against the old implementation the module/symbol does not exist, so every test fails with the
  literal marker ``QUIET001_QUIESCENT_CONVERGENCE_MISSING``;
- once the primitive is added, the same tests must pass.

The locked contract (pure, offline, deterministic, fail closed):

- input: a plain mapping describing each pre-convergence activity's resolution. The four
  activities, in fixed clause order, are ``mutation``, ``waiting``, ``drift_reconciliation``,
  and ``side_effect_outcome``;
- an activity is *resolved* only when its resolution value is the boolean ``True``. Any other
  value -- ``False``, ``None``, a status string, or an absent key -- is *unresolved* (fail
  closed), so an absent/unknown resolution is always reported as unresolved;
- output: a tuple of the unresolved activities' clause names, always in the fixed order
  ``mutation``, ``waiting``, ``drift_reconciliation``, ``side_effect_outcome``. A fully
  resolved activity set yields the empty tuple (quiescent), and repeated evaluation of the
  same input is byte-identical.

No network, model, provider, subprocess, git, or filesystem access in the tested paths.
"""

from __future__ import annotations

import importlib
from collections.abc import Callable
from typing import cast

import pytest

_MISSING = "QUIET001_QUIESCENT_CONVERGENCE_MISSING"
_MODULE = "converge_orchestrator.quiescence"
_NAME = "evaluate_terminal_quiescence"

_CLAUSE_MUTATION = "mutation"
_CLAUSE_WAITING = "waiting"
_CLAUSE_DRIFT_RECONCILIATION = "drift_reconciliation"
_CLAUSE_SIDE_EFFECT_OUTCOME = "side_effect_outcome"

_FIXED_ORDER: tuple[str, ...] = (
    _CLAUSE_MUTATION,
    _CLAUSE_WAITING,
    _CLAUSE_DRIFT_RECONCILIATION,
    _CLAUSE_SIDE_EFFECT_OUTCOME,
)

# A pure predicate: a plain activity-resolution mapping -> the (possibly empty) tuple of
# unresolved clauses, always returned in the fixed clause order.
Quiescence = Callable[[object], tuple[str, ...]]


def _evaluate() -> Quiescence:
    """Resolve the INV-QUIET-001 primitive, failing with the RED marker if it is absent.

    Every test routes through this helper first, so against the old tree (where
    ``converge_orchestrator.quiescence`` does not exist) each test fails with the literal
    ``QUIET001_QUIESCENT_CONVERGENCE_MISSING`` marker rather than an opaque import error.
    """
    try:
        module = importlib.import_module(_MODULE)
    except ModuleNotFoundError as exc:
        raise AssertionError(
            f"{_MISSING}: {_MODULE}.{_NAME} is not defined; "
            "INV-QUIET-001 requires a pure, deterministic terminal-quiescence predicate that "
            "reports every unresolved pre-convergence activity (mutation, waiting, drift "
            "reconciliation, side-effect outcome) in a fixed clause order."
        ) from exc
    primitive = getattr(module, _NAME, None)
    assert callable(primitive), (
        f"{_MISSING}: {_MODULE}.{_NAME} is not defined; "
        "INV-QUIET-001 requires a terminal-quiescence predicate for converged status."
    )
    return cast(Quiescence, primitive)


def _fully_resolved() -> dict[str, object]:
    """A plain activity-resolution mapping in which every activity is definitively resolved."""
    return {name: True for name in _FIXED_ORDER}


def test_fully_resolved_activity_set_is_quiescent() -> None:
    evaluate = _evaluate()
    assert evaluate(_fully_resolved()) == ()


@pytest.mark.parametrize("activity", list(_FIXED_ORDER))
def test_each_unresolved_activity_reports_its_named_clause(activity: str) -> None:
    evaluate = _evaluate()
    state = _fully_resolved()
    state[activity] = False
    assert evaluate(state) == (activity,)


def test_multiple_unresolved_activities_are_returned_in_fixed_order() -> None:
    evaluate = _evaluate()
    state = {name: False for name in _FIXED_ORDER}
    assert evaluate(state) == _FIXED_ORDER


def test_fixed_order_is_independent_of_input_mapping_order() -> None:
    # The input mapping lists the side-effect outcome first; the output must still follow the
    # fixed clause order (mutation before side-effect outcome).
    evaluate = _evaluate()
    state = {
        _CLAUSE_SIDE_EFFECT_OUTCOME: False,
        _CLAUSE_WAITING: True,
        _CLAUSE_MUTATION: False,
        _CLAUSE_DRIFT_RECONCILIATION: True,
    }
    assert evaluate(state) == (_CLAUSE_MUTATION, _CLAUSE_SIDE_EFFECT_OUTCOME)


def test_absent_activity_resolution_fails_closed() -> None:
    evaluate = _evaluate()
    state = _fully_resolved()
    del state[_CLAUSE_WAITING]
    assert evaluate(state) == (_CLAUSE_WAITING,)


@pytest.mark.parametrize("resolution", [None, "pending", "unknown"])
def test_unknown_resolution_value_fails_closed(resolution: object) -> None:
    evaluate = _evaluate()
    state = _fully_resolved()
    state[_CLAUSE_DRIFT_RECONCILIATION] = resolution
    assert evaluate(state) == (_CLAUSE_DRIFT_RECONCILIATION,)


def test_evaluation_is_deterministic_and_returns_a_tuple() -> None:
    evaluate = _evaluate()
    state = {
        _CLAUSE_MUTATION: False,
        _CLAUSE_WAITING: True,
        _CLAUSE_DRIFT_RECONCILIATION: False,
        _CLAUSE_SIDE_EFFECT_OUTCOME: True,
    }
    first = evaluate(state)
    second = evaluate(state)
    assert isinstance(first, tuple)
    assert first == second
    assert first == (_CLAUSE_MUTATION, _CLAUSE_DRIFT_RECONCILIATION)
