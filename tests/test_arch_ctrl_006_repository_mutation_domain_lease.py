"""RED lock test for ARCH-CTRL-006: domain-keyed repository mutation lease guard.

ARCH-CTRL-006 (CONVERGE_ORCHESTRATOR_2_0_SOURCE_OF_TRUTH.md:L244 [7. Deterministic architecture
and agent roles]): Repository mutation lease and fencing authority SHALL be keyed by repository
mutation domain identity rather than project ID alone and SHALL be authoritative across every
controller permitted to mutate that domain.

These deterministic offline tests lock the not-yet-existing pure primitive
``converge_orchestrator.mutation_domain_lease.evaluate_repository_mutation_lease``:

- against the old implementation the module/symbol does not exist, so every test fails with the
  literal marker ``ARCHCTRL006_REPOSITORY_MUTATION_DOMAIN_LEASE_MISSING``;
- once the primitive is added, the same tests must pass. It takes a plain mapping describing the
  controller's claimed repository mutation domain identity and lease epoch together with the
  authoritative domain identity and current lease epoch. It returns the lost-lease clauses in a
  fixed order:

  * ``"domain_identity_lost"`` -- the controller's claimed mutation-domain identity is absent or
    differs from the authoritative repository mutation domain identity;
  * ``"lease_epoch_lost"`` -- the controller's claimed lease epoch is absent, stale, or
    mismatched against the authoritative current lease epoch.

A controller whose claimed domain identity and lease epoch both match the authoritative values
yields an empty result. Non-mapping input fails closed with ``TypeError``. All inputs are plain
values so the predicate stays pure and deterministic in memory. No network, model, provider,
subprocess, git, or filesystem side effects in the tested path.
"""

from __future__ import annotations

import importlib
from collections.abc import Callable, Iterable
from typing import cast

import pytest

_MISSING = "ARCHCTRL006_REPOSITORY_MUTATION_DOMAIN_LEASE_MISSING"
_MODULE = "converge_orchestrator.mutation_domain_lease"
_NAME = "evaluate_repository_mutation_lease"

_CLAUSE_DOMAIN_IDENTITY_LOST = "domain_identity_lost"
_CLAUSE_LEASE_EPOCH_LOST = "lease_epoch_lost"

_DOMAIN_A = "mutdom-aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
_DOMAIN_B = "mutdom-bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"
_DOMAIN_C = "mutdom-cccccccccccccccccccccccccccccccccccccccccccccccccccccccc"

# A pure guard: a plain repository mutation lease mapping -> the (possibly empty) iterable of
# lost-lease clauses, in the fixed order domain identity first, then lease epoch.
Guard = Callable[[object], Iterable[str]]


def _guard() -> Guard:
    """Resolve the ARCH-CTRL-006 primitive, failing with the RED marker if it is absent.

    Every test routes through this helper first, so against the old tree (where
    ``converge_orchestrator.mutation_domain_lease`` does not exist) each test fails with the
    literal ``ARCHCTRL006_REPOSITORY_MUTATION_DOMAIN_LEASE_MISSING`` marker rather than an opaque
    import error.
    """
    try:
        module = importlib.import_module(_MODULE)
    except ModuleNotFoundError as exc:
        raise AssertionError(
            f"{_MISSING}: {_MODULE}.{_NAME} is not defined; "
            "ARCH-CTRL-006 requires a deterministic, offline repository mutation lease guard that "
            "is keyed by repository mutation domain identity and fails closed whenever the "
            "claimed domain identity or lease epoch does not match the authoritative values."
        ) from exc
    guard = getattr(module, _NAME, None)
    assert callable(guard), (
        f"{_MISSING}: {_MODULE}.{_NAME} is not defined; "
        "ARCH-CTRL-006 requires a deterministic repository mutation lease guard primitive."
    )
    return cast(Guard, guard)


def _state(
    *,
    claimed_domain: object = _DOMAIN_A,
    authoritative_domain: object = _DOMAIN_A,
    claimed_epoch: object = 3,
    authoritative_epoch: object = 3,
) -> dict[str, object]:
    """Build a plain repository mutation lease mapping with no coupling to controller state."""
    return {
        "claimed_mutation_domain_id": claimed_domain,
        "authoritative_mutation_domain_id": authoritative_domain,
        "claimed_lease_epoch": claimed_epoch,
        "authoritative_lease_epoch": authoritative_epoch,
    }


def test_matching_identity_and_epoch_report_no_lost_clauses() -> None:
    guard = _guard()
    # The claimed mutation-domain identity and lease epoch both match the authoritative values,
    # so the controller still holds the repository mutation lease for that domain.
    assert tuple(guard(_state())) == ()


def test_mismatched_domain_identity_reports_domain_identity_lost() -> None:
    guard = _guard()
    # A claimed domain identity that differs from the authoritative one cannot prove ownership
    # of the domain lease, so the domain-identity clause is reported (the epoch clause still
    # matches and is not reported).
    assert tuple(guard(_state(claimed_domain=_DOMAIN_B))) == (_CLAUSE_DOMAIN_IDENTITY_LOST,)


def test_absent_domain_identity_reports_domain_identity_lost() -> None:
    guard = _guard()
    # An absent (None) claimed domain identity cannot prove ownership of any domain lease, so the
    # guard fails closed and reports the domain-identity clause.
    assert tuple(guard(_state(claimed_domain=None))) == (_CLAUSE_DOMAIN_IDENTITY_LOST,)


def test_stale_lease_epoch_reports_lease_epoch_lost() -> None:
    guard = _guard()
    # The controller claims a lease epoch older than the authoritative current one: it no longer
    # holds the current lease, so the epoch clause is reported (the domain identity still holds).
    assert tuple(guard(_state(claimed_epoch=1, authoritative_epoch=5))) == (
        _CLAUSE_LEASE_EPOCH_LOST,
    )


def test_mismatched_lease_epoch_reports_lease_epoch_lost() -> None:
    guard = _guard()
    # The claimed lease epoch differs from the authoritative current one in either direction, so
    # the epoch clause is still reported.
    assert tuple(guard(_state(claimed_epoch=7, authoritative_epoch=5))) == (
        _CLAUSE_LEASE_EPOCH_LOST,
    )


def test_absent_lease_epoch_reports_lease_epoch_lost() -> None:
    guard = _guard()
    # An absent (None) claimed lease epoch cannot prove current-lease ownership, so the guard
    # fails closed and reports the epoch clause.
    assert tuple(guard(_state(claimed_epoch=None))) == (_CLAUSE_LEASE_EPOCH_LOST,)


def test_both_lost_clauses_report_in_fixed_order() -> None:
    guard = _guard()
    # Both the domain identity and the lease epoch are lost; the clauses are reported in the fixed
    # order: domain identity first, then lease epoch.
    assert tuple(
        guard(_state(claimed_domain=_DOMAIN_C, claimed_epoch=1, authoritative_epoch=5))
    ) == (_CLAUSE_DOMAIN_IDENTITY_LOST, _CLAUSE_LEASE_EPOCH_LOST)


def test_non_mapping_input_raises_type_error() -> None:
    guard = _guard()
    # The guard is a decision predicate over a mapping; non-mapping input must fail closed with
    # TypeError rather than silently returning a false-negative result.
    with pytest.raises(TypeError):
        tuple(guard("not a mapping"))
