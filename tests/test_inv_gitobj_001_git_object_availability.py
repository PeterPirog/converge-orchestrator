"""RED lock test for INV-GITOBJ-001: policy-bound Git object availability.

INV-GITOBJ-001 (CONVERGE_ORCHESTRATOR_2_0_SOURCE_OF_TRUTH.md:L175 [4. Global invariants]):
Candidate readiness, accepted integration, and terminal convergence SHALL NOT depend on a Git
object available only through undeclared local alternates, stale promisor storage, incomplete
shallow history, or another repository-object source not bound by project policy.

The codebase has no primitive that decides, for the Git objects candidate readiness, accepted
integration, and terminal convergence depend on, whether every required object is reachable
through at least one policy-bound source. These deterministic offline tests therefore lock the
not-yet-existing pure primitive ``converge_orchestrator.git.GitObjectAvailability``:

- against the old implementation the module has no such symbol, so every test fails with the
  literal marker ``GITOBJ001_GIT_OBJECT_AVAILABILITY_MISSING``;
- once the primitive is added, the same tests must pass. It is constructed from the required
  Git object ids (``required``) and, for each of those ids, the sources the object is reachable
  through (``sources``), where each source records its ``kind`` (an undeclared local alternate,
  stale promisor storage, incomplete shallow history, another unbound repository-object source,
  or a policy-bound canonical object store) and whether it is ``policy_bound``. It reports
  ``ready`` (``True`` only when no required object is reachable only through an unbound source)
  and ``unbound`` (a stably ordered, sorted tuple of exactly the required object ids that have
  no policy-bound source):

  * fail-closed -- a required object available only through an undeclared local alternate,
    stale promisor storage, incomplete shallow history, or another unbound repository-object
    source (or an absent object with no sources at all) keeps ``ready`` False and appears in
    ``unbound``;
  * policy-bound disjunct -- a required object that is reachable through at least one
    policy-bound source is admitted (``ready`` True, absent from ``unbound``) even when it is
    also present in one or more unbound sources.

No network, model, provider, subprocess, git, or filesystem side effects: the checks are pure,
deterministic in-memory operations over plain values and collections.
"""

from __future__ import annotations

import converge_orchestrator.git as git

_MISSING = "GITOBJ001_GIT_OBJECT_AVAILABILITY_MISSING"
_NAME = "GitObjectAvailability"


def _availability_class() -> type:
    """Resolve the INV-GITOBJ-001 primitive, failing with the RED marker if it is absent.

    Every test routes through this helper first, so against the old tree (where
    ``converge_orchestrator.git`` has no ``GitObjectAvailability`` symbol) each test fails with
    the literal ``GITOBJ001_GIT_OBJECT_AVAILABILITY_MISSING`` marker rather than an opaque
    AttributeError.
    """
    cls = getattr(git, _NAME, None)
    assert cls is not None, (
        f"{_MISSING}: converge_orchestrator.git.{_NAME} is not defined; "
        "INV-GITOBJ-001 requires a pure, deterministic, in-memory Git object availability "
        "primitive that reports ``ready`` only when every required Git object is reachable "
        "through at least one policy-bound source, fails closed for any required object "
        "available only through an undeclared local alternate, stale promisor storage, "
        "incomplete shallow history, or another repository-object source not bound by project "
        "policy (including an absent object), and exposes ``unbound`` as a stably ordered "
        "(sorted) tuple of exactly the required object ids that have no policy-bound source."
    )
    return cls


def _source(kind: str, *, policy_bound: bool) -> dict[str, object]:
    """Build a plain source record: its category and whether project policy binds it."""
    return {"kind": kind, "policy_bound": policy_bound}


def _bound() -> dict[str, object]:
    """A source that IS bound by project policy (e.g. the canonical object store)."""
    return _source("canonical_object_store", policy_bound=True)


def _unbound(kind: str) -> dict[str, object]:
    """A source NOT bound by project policy in the given unbound category."""
    return _source(kind, policy_bound=False)


def test_undeclared_local_alternate_only_is_not_ready() -> None:
    """Fail-closed: an object available only via an undeclared local alternate is not ready."""
    availability = _availability_class()(
        required=("object-a",),
        sources={"object-a": [_unbound("undeclared_local_alternate")]},
    )
    assert availability.ready is False
    assert availability.unbound == ("object-a",)


def test_stale_promisor_storage_only_is_not_ready() -> None:
    """Fail-closed: an object available only via stale promisor storage is not ready."""
    availability = _availability_class()(
        required=("object-a",),
        sources={"object-a": [_unbound("stale_promisor_storage")]},
    )
    assert availability.ready is False
    assert availability.unbound == ("object-a",)


def test_incomplete_shallow_history_only_is_not_ready() -> None:
    """Fail-closed: an object available only via incomplete shallow history is not ready."""
    availability = _availability_class()(
        required=("object-a",),
        sources={"object-a": [_unbound("incomplete_shallow_history")]},
    )
    assert availability.ready is False
    assert availability.unbound == ("object-a",)


def test_other_unbound_repository_object_source_only_is_not_ready() -> None:
    """Fail-closed: an object available only via another unbound repository-object source."""
    availability = _availability_class()(
        required=("object-a",),
        sources={"object-a": [_unbound("other_unbound_source")]},
    )
    assert availability.ready is False
    assert availability.unbound == ("object-a",)


def test_absent_required_object_fails_closed() -> None:
    """Fail-closed: a required object with no sources at all is not ready and is unbound."""
    availability = _availability_class()(
        required=("object-a",),
        sources={},
    )
    assert availability.ready is False
    assert availability.unbound == ("object-a",)


def test_policy_bound_source_admits_object_also_in_unbound_source() -> None:
    """Policy-bound disjunct: a bound source admits an object that is also in an unbound source."""
    availability = _availability_class()(
        required=("object-a",),
        sources={
            "object-a": [
                _bound(),
                _unbound("stale_promisor_storage"),
                _unbound("incomplete_shallow_history"),
            ],
        },
    )
    assert availability.ready is True
    assert availability.unbound == ()


def test_ready_when_every_required_object_is_policy_bound() -> None:
    """Positive control: every required object reachable through a policy-bound source."""
    availability = _availability_class()(
        required=("object-a", "object-b"),
        sources={
            "object-a": [_bound()],
            "object-b": [_bound(), _unbound("undeclared_local_alternate")],
        },
    )
    assert availability.ready is True
    assert availability.unbound == ()


def test_unbound_is_stably_sorted_tuple_of_unbound_objects() -> None:
    """``unbound`` is the sorted tuple of exactly the required object ids with no bound source."""
    availability = _availability_class()(
        required=("mid", "zeta", "alpha"),
        sources={
            "mid": [_unbound("stale_promisor_storage")],
            "zeta": [_bound()],
            "alpha": [_unbound("incomplete_shallow_history")],
        },
    )
    assert availability.ready is False
    # "zeta" is policy-bound and therefore excluded; "alpha" and "mid" remain, in sorted order.
    assert availability.unbound == ("alpha", "mid")
