"""INV-CAMPAIGN-001: deterministic campaign run-transition guard.

Pure, offline primitives for the invariant
INV-CAMPAIGN-001 (CONVERGE_ORCHESTRATOR_2_0_SOURCE_OF_TRUTH.md:L169 [4. Global invariants]):
automatic transition from one run to another inside a convergence campaign SHALL NOT reset
campaign-level resource authority, transfer a live mutation candidate, or reinterpret
unfinished work as a fresh independent success opportunity.

``evaluate_run_transition`` inspects a plain mapping describing a prior run and a successor
run of one convergence campaign and returns the violated clauses. Inputs and outputs are
plain values so the guard can later be bound to registry run rows. There is no network,
model, provider, subprocess, git-remote, or filesystem access in this module.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import TypeGuard, cast

#: INV-CAMPAIGN-001 clause 1: the transition resets campaign-level resource authority
#: (the authority identity changes, or a campaign-level consumed counter decreases).
RESOURCE_AUTHORITY_RESET = "resource_authority_reset"
#: INV-CAMPAIGN-001 clause 2: an unfinished prior run's live (active) mutation candidate
#: is taken over by the successor run.
LIVE_MUTATION_CANDIDATE_TRANSFER = "live_mutation_candidate_transfer"
#: INV-CAMPAIGN-001 clause 3: an unfinished prior run is reinterpreted as an independent,
#: fully-successful run that grants a fresh success opportunity.
UNFINISHED_WORK_AS_FRESH_SUCCESS = "unfinished_work_as_fresh_success"

#: Run statuses meaning the run's work was fully and successfully completed. Any other
#: status (or a missing one) leaves the work unfinished: without positive evidence of
#: success, an invariant guard must not assume the work is done.
_SUCCESS_TERMINAL_STATUSES = frozenset({"converged", "completed", "success", "succeeded"})

#: Plain-mapping field names describing a run for transition inspection.
_FIELD_STATUS = "status"
_FIELD_AUTHORITY_IDENTITY = "campaign_authority_identity"
_FIELD_AUTHORITY_CONSUMED = "authority_consumed"
_FIELD_ACTIVE_CANDIDATE = "active_mutation_candidate_id"
_FIELD_FRESH_SUCCESS = "grants_fresh_success_opportunity"


def _run_fields(transition: Mapping[str, object], key: str) -> Mapping[str, object]:
    run = transition.get(key)
    if not isinstance(run, Mapping):
        raise TypeError(f"run transition {key!r} must be a mapping of run fields")
    return cast("Mapping[str, object]", run)


def _is_unfinished_work(run: Mapping[str, object]) -> bool:
    """Return True when the run's work was not fully and successfully completed."""
    status = run.get(_FIELD_STATUS)
    return not (isinstance(status, str) and status in _SUCCESS_TERMINAL_STATUSES)


def _is_count(value: object) -> TypeGuard[int]:
    return isinstance(value, int) and not isinstance(value, bool)


def _authority_reset(prior: Mapping[str, object], successor: Mapping[str, object]) -> bool:
    """Clause 1: campaign-level resource authority is reset across the transition."""
    if prior.get(_FIELD_AUTHORITY_IDENTITY) != successor.get(_FIELD_AUTHORITY_IDENTITY):
        return True
    prior_consumed = prior.get(_FIELD_AUTHORITY_CONSUMED)
    successor_consumed = successor.get(_FIELD_AUTHORITY_CONSUMED)
    return (
        _is_count(prior_consumed)
        and _is_count(successor_consumed)
        and successor_consumed < prior_consumed
    )


def _candidate_transfer(prior: Mapping[str, object], successor: Mapping[str, object]) -> bool:
    """Clause 2: the successor run takes over the prior run's live mutation candidate."""
    candidate = prior.get(_FIELD_ACTIVE_CANDIDATE)
    if candidate is None:
        return False
    return successor.get(_FIELD_ACTIVE_CANDIDATE) == candidate


def _fresh_success_reinterpretation(successor: Mapping[str, object]) -> bool:
    """Clause 3: the successor grants a fresh success opportunity independent of the prior run."""
    return successor.get(_FIELD_FRESH_SUCCESS) is True


def evaluate_run_transition(transition: Mapping[str, object]) -> tuple[str, ...]:
    """Deterministically flag the INV-CAMPAIGN-001 clauses violated by a run transition.

    ``transition`` maps ``"prior"`` and ``"successor"`` to plain run-field mappings with the
    optional keys ``status``, ``campaign_authority_identity``, ``authority_consumed``,
    ``active_mutation_candidate_id``, and ``grants_fresh_success_opportunity``.

    Returns the violated clauses in fixed clause order (an empty tuple when the transition
    is compliant):

    - ``RESOURCE_AUTHORITY_RESET`` when the campaign authority identity changes or a
      campaign-level consumed counter decreases from the prior run to the successor run;
    - ``LIVE_MUTATION_CANDIDATE_TRANSFER`` when an unfinished prior run still holds an
      active mutation candidate that the successor run takes over;
    - ``UNFINISHED_WORK_AS_FRESH_SUCCESS`` when an unfinished prior run is reinterpreted
      as an independent, fully-successful run granting a fresh success opportunity.
    """
    if not isinstance(transition, Mapping):
        raise TypeError("run transition must be a mapping with 'prior' and 'successor' runs")
    prior = _run_fields(transition, "prior")
    successor = _run_fields(transition, "successor")

    violations: list[str] = []
    if _authority_reset(prior, successor):
        violations.append(RESOURCE_AUTHORITY_RESET)
    if _is_unfinished_work(prior) and _candidate_transfer(prior, successor):
        violations.append(LIVE_MUTATION_CANDIDATE_TRANSFER)
    if _is_unfinished_work(prior) and _fresh_success_reinterpretation(successor):
        violations.append(UNFINISHED_WORK_AS_FRESH_SUCCESS)
    return tuple(violations)
