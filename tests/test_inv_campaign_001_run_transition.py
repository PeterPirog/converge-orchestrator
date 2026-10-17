"""RED lock test for INV-CAMPAIGN-001: deterministic campaign run-transition guard.

INV-CAMPAIGN-001 (CONVERGE_ORCHESTRATOR_2_0_SOURCE_OF_TRUTH.md:L169 [4. Global invariants]):
Automatic transition from one run to another inside a convergence campaign SHALL NOT reset
campaign-level resource authority, transfer a live mutation candidate, or reinterpret unfinished
work as a fresh independent success opportunity.

The codebase has no campaign run-transition primitive. These deterministic offline tests therefore
lock the not-yet-existing pure primitive ``converge_orchestrator.campaign.evaluate_run_transition``:

- against the old implementation the module/symbol does not exist, so every test fails with the
  literal marker ``CAMPAIGN001_RUN_TRANSITION_GUARD_MISSING``;
- once the primitive is added, the same tests must pass. It takes a plain mapping describing a
  prior run and a successor run of one convergence campaign and returns the violated clauses:

  * ``"resource_authority_reset"``     -- clause 1: the transition resets campaign-level resource
    authority (the campaign authority identity changes, or a campaign-level consumed counter
    decreases from the prior run to the successor run);
  * ``"live_mutation_candidate_transfer"`` -- clause 2: an unfinished prior run still holds a live
    (active) mutation candidate that the successor run takes over;
  * ``"unfinished_work_as_fresh_success"`` -- clause 3: an unfinished prior run is reinterpreted as
    an independent, fully-successful run that grants a fresh success opportunity.

A compliant transition (continuous campaign authority, no live-candidate transfer, and unfinished
prior work still recognized as unfinished) yields an empty result. All inputs are plain mappings so
the primitive can later be bound to registry run rows. No network, model, provider, subprocess,
git-remote, or filesystem access in the tested paths.
"""

from __future__ import annotations

import importlib
from collections.abc import Callable, Iterable
from typing import cast

_MISSING = "CAMPAIGN001_RUN_TRANSITION_GUARD_MISSING"
_MODULE = "converge_orchestrator.campaign"
_NAME = "evaluate_run_transition"

_VIOLATION_RESOURCE_AUTHORITY_RESET = "resource_authority_reset"
_VIOLATION_LIVE_CANDIDATE_TRANSFER = "live_mutation_candidate_transfer"
_VIOLATION_UNFINISHED_AS_FRESH_SUCCESS = "unfinished_work_as_fresh_success"

# A pure guard: a plain transition mapping -> the (possibly empty) iterable of violated clauses.
Guard = Callable[[object], Iterable[str]]


def _guard() -> Guard:
    """Resolve the INV-CAMPAIGN-001 primitive, failing with the RED marker if it is absent.

    Every test routes through this helper first, so against the old tree (where
    ``converge_orchestrator.campaign`` does not exist) each test fails with the literal
    ``CAMPAIGN001_RUN_TRANSITION_GUARD_MISSING`` marker rather than an opaque import error.
    """
    try:
        module = importlib.import_module(_MODULE)
    except ModuleNotFoundError as exc:
        raise AssertionError(
            f"{_MISSING}: {_MODULE}.{_NAME} is not defined; "
            "INV-CAMPAIGN-001 requires a deterministic, offline campaign run-transition guard "
            "that flags (1) a reset of campaign-level resource authority, (2) a transfer of a "
            "live mutation candidate, or (3) unfinished work reinterpreted as a fresh "
            "independent success opportunity."
        ) from exc
    guard = getattr(module, _NAME, None)
    assert callable(guard), (
        f"{_MISSING}: {_MODULE}.{_NAME} is not defined; "
        "INV-CAMPAIGN-001 requires a deterministic campaign run-transition guard primitive."
    )
    return cast(Guard, guard)


def _run(
    *,
    status: str = "unfinished",
    authority: str = "auth-A",
    consumed: int = 10,
    candidate: str | None = None,
    grants_fresh_success: bool = False,
) -> dict[str, object]:
    """Build a plain run mapping with no coupling to any concrete registry run row."""
    return {
        "status": status,
        "campaign_authority_identity": authority,
        "authority_consumed": consumed,
        "active_mutation_candidate_id": candidate,
        "grants_fresh_success_opportunity": grants_fresh_success,
    }


def test_resource_authority_reset_on_consumed_counter_decrease() -> None:
    guard = _guard()
    transition = {
        "prior": _run(consumed=10),
        "successor": _run(status="in-progress", consumed=4),
    }
    assert set(guard(transition)) == {_VIOLATION_RESOURCE_AUTHORITY_RESET}


def test_resource_authority_reset_on_authority_identity_change() -> None:
    guard = _guard()
    transition = {
        "prior": _run(authority="auth-A"),
        "successor": _run(status="in-progress", authority="auth-B"),
    }
    assert set(guard(transition)) == {_VIOLATION_RESOURCE_AUTHORITY_RESET}


def test_live_mutation_candidate_transfer_flagged() -> None:
    guard = _guard()
    transition = {
        "prior": _run(status="unfinished", candidate="cand-A"),
        "successor": _run(status="in-progress", candidate="cand-A"),
    }
    assert set(guard(transition)) == {_VIOLATION_LIVE_CANDIDATE_TRANSFER}


def test_unfinished_work_reinterpreted_as_fresh_success_flagged() -> None:
    guard = _guard()
    transition = {
        "prior": _run(status="unfinished"),
        "successor": _run(status="in-progress", grants_fresh_success=True),
    }
    assert set(guard(transition)) == {_VIOLATION_UNFINISHED_AS_FRESH_SUCCESS}


def test_compliant_transition_reports_no_violations() -> None:
    guard = _guard()
    transition = {
        "prior": _run(status="unfinished", consumed=10),
        "successor": _run(status="in-progress", consumed=11),
    }
    assert set(guard(transition)) == set()
