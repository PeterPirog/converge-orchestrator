"""INV-CAMPAIGN-001: deterministic campaign run-transition guard.

Pure, offline primitives for the invariant
INV-CAMPAIGN-001 (CONVERGE_ORCHESTRATOR_2_0_SOURCE_OF_TRUTH.md:L169 [4. Global invariants]):
automatic transition from one run to another inside a convergence campaign SHALL NOT reset
campaign-level resource authority, transfer a live mutation candidate, or reinterpret
unfinished work as a fresh independent success opportunity.

``evaluate_run_transition`` inspects a plain mapping describing a prior run and a successor
run of one convergence campaign and returns the violated clauses. Inputs and outputs are
plain values so the guard can later be bound to registry run rows.

REQ-CONFIG-016 (CONVERGE_ORCHESTRATOR_2_0_SOURCE_OF_TRUTH.md:L214
[6. Project configuration and secret sources]): a convergence campaign SHALL pin a
campaign-policy identity covering its Source-of-Truth digest, integration line,
cumulative resource envelope, interaction policy, and the minimum gate, review, security,
evidence, and remote-integration semantics that successor runs are not permitted to weaken.
``campaign_policy_identity`` computes that identity as a pure, deterministic SHA-256 over a
canonical serialization of the nine covered components, and
``evaluate_campaign_policy_continuation`` fails closed when a successor run carries a
different identity; no network, model, provider, subprocess, git, or filesystem access occurs.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
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

#: REQ-CONFIG-016: the successor run carries a campaign-policy identity that differs from
#: the pinned campaign identity, i.e. it would weaken a pinned minimum semantic.
CAMPAIGN_POLICY_IDENTITY_CHANGED = "campaign_policy_identity_changed"

#: REQ-CONFIG-016: the nine campaign-policy components the pinned identity covers. A
#: component missing from, ``None`` in, or blank in a policy mapping fails closed.
_CAMPAIGN_POLICY_COMPONENTS = (
    "source_of_truth_digest",
    "integration_line",
    "cumulative_resource_envelope",
    "interaction_policy",
    "minimum_gate",
    "minimum_review",
    "minimum_security",
    "minimum_evidence",
    "minimum_remote_integration",
)

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


def _policy_error(component: str, detail: str) -> ValueError:
    return ValueError(
        f"campaign policy component {component!r} {detail}; REQ-CONFIG-016 "
        "campaign-policy identity fails closed"
    )


def _require_canonical_value(value: object, component: str, top_level: bool) -> None:
    """Fail closed when ``value`` is not a canonical campaign-policy component value."""
    if value is None:
        if top_level:
            raise _policy_error(component, "must be present and non-None")
        return
    if isinstance(value, str):
        if top_level and not value.strip():
            raise _policy_error(component, "must be a non-blank string")
        return
    if isinstance(value, float) and not math.isfinite(value):
        raise _policy_error(component, "must be a finite number")
    if isinstance(value, (bool, int, float)):
        return
    if isinstance(value, (list, Mapping)):
        if top_level and not value:
            raise _policy_error(component, "must be a non-empty list or mapping")
        if isinstance(value, Mapping) and not all(isinstance(key, str) for key in value):
            raise _policy_error(component, "must use string keys in mappings")
        for item in (value.values() if isinstance(value, Mapping) else value):
            _require_canonical_value(item, component, False)
        return
    raise _policy_error(
        component, "must be a string, bool, number, list, or mapping of canonical values"
    )


def campaign_policy_identity(policy: Mapping[str, object]) -> str:
    """Compute the REQ-CONFIG-016 campaign-policy identity of a complete policy mapping.

    ``policy`` maps each of the nine covered components (``source_of_truth_digest``,
    ``integration_line``, ``cumulative_resource_envelope``, ``interaction_policy``,
    ``minimum_gate``, ``minimum_review``, ``minimum_security``, ``minimum_evidence``, and
    ``minimum_remote_integration``) to a canonical value: a non-blank string, a bool, a
    finite number, a list, or a string-keyed mapping (nested recursively). A missing,
    ``None``, or blank component fails closed with a ``ValueError`` naming the component.

    Returns the lowercase SHA-256 hex digest (64 characters) of a canonical serialization of
    exactly the nine covered components; the computation is pure and deterministic, using
    standard-library hashing over sorted canonical JSON only, so equal mappings yield equal
    identities regardless of key insertion order, with no external access of any kind.
    """
    if not isinstance(policy, Mapping):
        raise TypeError("campaign policy must be a mapping of policy components")
    canonical = {}
    for name in _CAMPAIGN_POLICY_COMPONENTS:
        value = policy.get(name)
        _require_canonical_value(value, name, True)
        canonical[name] = value
    encoded = json.dumps(
        canonical, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def evaluate_campaign_policy_continuation(continuation: Mapping[str, object]) -> tuple[str, ...]:
    """Deterministically flag a successor run that weakens the pinned campaign policy.

    ``continuation`` maps ``"successor"`` to a campaign-policy mapping and ``"pinned"`` to
    either the pinned policy mapping or the durably recorded pinned identity digest (a
    64-character lowercase SHA-256 hex digest), so the successor is compared against the
    pin itself rather than a mutable copy of it.

    Returns an empty tuple when the successor carries the pinned identity, or exactly
    ``(CAMPAIGN_POLICY_IDENTITY_CHANGED,)`` when any covered component differs, so a
    successor run is never permitted to silently weaken pinned minimum semantics.
    """
    if not isinstance(continuation, Mapping):
        raise TypeError(
            "campaign policy continuation must be a mapping with 'pinned' and 'successor' policies"
        )
    successor = continuation.get("successor")
    if not isinstance(successor, Mapping):
        raise TypeError(
            "campaign policy continuation 'successor' must be a mapping of policy components"
        )
    pinned = continuation.get("pinned")
    if isinstance(pinned, str) and re.fullmatch(r"[0-9a-f]{64}", pinned):
        pinned_digest = pinned
    elif isinstance(pinned, Mapping):
        pinned_digest = campaign_policy_identity(cast("Mapping[str, object]", pinned))
    else:
        raise TypeError(
            "campaign policy continuation 'pinned' must be a policy mapping or a "
            "64-character lowercase SHA-256 hex digest"
        )
    successor_digest = campaign_policy_identity(cast("Mapping[str, object]", successor))
    return () if pinned_digest == successor_digest else (CAMPAIGN_POLICY_IDENTITY_CHANGED,)
