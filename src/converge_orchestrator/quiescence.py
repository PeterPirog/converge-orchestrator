"""INV-QUIET-001: deterministic terminal-convergence quiescence predicate.

Pure, offline primitives for the invariant
INV-QUIET-001 (CONVERGE_ORCHESTRATOR_2_0_SOURCE_OF_TRUTH.md:L178 [4. Global invariants]):
a terminal converged status SHALL represent a quiescent convergence state rather than a momentary
set of PASS records while mutation, waiting, drift reconciliation, or side-effect outcome remains
unresolved.

``evaluate_terminal_quiescence`` inspects a plain mapping describing how each pre-convergence
activity has resolved, and returns the unresolved activities in a fixed clause order. The predicate
fails closed: an activity is resolved only when its resolution value is the boolean ``True``; any
other value -- ``False``, ``None``, a status string, or an absent key -- leaves the activity
unresolved. A fully resolved activity set yields the empty tuple (quiescent), and repeated
evaluation of the same input is byte-identical. There is no network, model, provider, subprocess,
git, or filesystem access in this module.
"""

from __future__ import annotations

from collections.abc import Mapping

#: INV-QUIET-001 clause: a pre-convergence mutation is still unresolved, so the converged status
#: is not a quiescent convergence state.
MUTATION = "mutation"
#: INV-QUIET-001 clause: the run is still waiting on an unresolved condition, so the converged
#: status is not a quiescent convergence state.
WAITING = "waiting"
#: INV-QUIET-001 clause: detected drift has not yet been reconciled, so the converged status is
#: not a quiescent convergence state.
DRIFT_RECONCILIATION = "drift_reconciliation"
#: INV-QUIET-001 clause: a side effect's outcome is still unresolved, so the converged status is
#: not a quiescent convergence state.
SIDE_EFFECT_OUTCOME = "side_effect_outcome"

#: Fixed clause order for the terminal-quiescence predicate, first to last. This order is stable
#: and independent of the input mapping's key order.
_CLAUSE_ORDER: tuple[str, ...] = (
    MUTATION,
    WAITING,
    DRIFT_RECONCILIATION,
    SIDE_EFFECT_OUTCOME,
)


def _is_resolved(resolution: object) -> bool:
    """Return True only when an activity is definitively resolved.

    Fails closed: an activity is resolved only when its resolution value is the boolean ``True``.
    Any other value -- ``False``, ``None``, a status string, or an absent key (``None``) -- is
    treated as unresolved, so an absent/unknown resolution is always reported as unresolved.
    """
    return resolution is True


def evaluate_terminal_quiescence(state: Mapping[str, object]) -> tuple[str, ...]:
    """Deterministically report the unresolved pre-convergence activities.

    ``state`` maps each pre-convergence activity name to its resolution value (``True`` when the
    activity is definitively resolved). The recognized activities are ``MUTATION``, ``WAITING``,
    ``DRIFT_RECONCILIATION``, and ``SIDE_EFFECT_OUTCOME``.

    Returns the unresolved activities' clause names in fixed clause order (an empty tuple when
    every activity is resolved and the convergence state is therefore quiescent):

    - ``MUTATION`` when the pre-convergence mutation is not definitively resolved;
    - ``WAITING`` when the waiting state is not definitively resolved;
    - ``DRIFT_RECONCILIATION`` when the drift reconciliation is not definitively resolved;
    - ``SIDE_EFFECT_OUTCOME`` when the side-effect outcome is not definitively resolved.

    A caller SHALL treat a non-empty result as a non-quiescent state and SHALL NOT report the run
    as terminal-converged (INV-QUIET-001).
    """
    if not isinstance(state, Mapping):
        raise TypeError("terminal quiescence state must be a mapping of activity resolutions")
    unresolved = [clause for clause in _CLAUSE_ORDER if not _is_resolved(state.get(clause))]
    return tuple(unresolved)
