"""RED lock test for INV-EVID-003: evidence closure for terminal convergence.

INV-EVID-003 (CONVERGE_ORCHESTRATOR_2_0_SOURCE_OF_TRUTH.md:L174 [4. Global invariants]):
Terminal convergence SHALL have evidence closure for every evidence dependency used by
mandatory PASS, integration, operational acceptance, and release provenance.

The codebase has no primitive that computes the transitive evidence-dependency closure of the
records used in those convergence contexts and reports every dependency with no resolved
evidence. These deterministic offline tests therefore lock the not-yet-existing primitive
``converge_orchestrator.evidence.EvidenceClosure``:

- against the old implementation the module has no such symbol, so every test fails with the
  literal marker ``EVID003_EVIDENCE_CLOSURE_MISSING``;
- once the primitive is added, the same tests must pass: the closure reaches every transitively
  reachable evidence dependency of the convergence records, reports the exact, stably ordered
  tuple of dependencies with no resolved evidence, and ``closed`` is ``True`` only when every
  one of them is resolved (a partial closure is ``closed=False``).

``EvidenceClosure`` is constructed from (a) the root evidence record ids used by the convergence
contexts, (b) a mapping of an evidence id to the ids it depends on, and (c) the ids that have
resolved evidence. It exposes ``closure`` (the full transitively reachable set, stably ordered),
``missing`` (the reachable dependencies with no resolved evidence, stably ordered), and ``closed``
(``True`` only when ``missing`` is empty).

Closure computation is pure and deterministic: no network, model, provider, subprocess, or
filesystem side effects, and ordering is a stable sort of the evidence ids so repeated evaluation
is byte-identical.
"""

from __future__ import annotations

import converge_orchestrator.evidence as evidence

_MISSING = "EVID003_EVIDENCE_CLOSURE_MISSING"


def _closure() -> type:
    """Resolve the INV-EVID-003 primitive, failing with the RED marker if it is absent."""
    primitive = getattr(evidence, "EvidenceClosure", None)
    assert primitive is not None, (
        f"{_MISSING}: converge_orchestrator.evidence.EvidenceClosure is not defined; "
        "INV-EVID-003 requires terminal convergence to have evidence closure for every "
        "evidence dependency used by mandatory PASS, integration, operational acceptance, and "
        "release provenance."
    )
    return primitive


def test_closed_true_only_when_every_dependency_is_resolved() -> None:
    Closure = _closure()
    result = Closure(
        root_ids=("evid:pass",),
        dependencies={
            "evid:pass": ("evid:dep-a", "evid:dep-b"),
            "evid:dep-a": ("evid:dep-c",),
        },
        resolved=("evid:pass", "evid:dep-a", "evid:dep-b", "evid:dep-c"),
    )
    assert result.closed is True
    assert result.missing == ()


def test_partial_closure_is_not_closed_and_reports_the_unresolved_dependency() -> None:
    Closure = _closure()
    result = Closure(
        root_ids=("evid:pass",),
        dependencies={
            "evid:pass": ("evid:dep-a", "evid:dep-b"),
            "evid:dep-a": ("evid:dep-c",),
        },
        resolved=("evid:pass", "evid:dep-a", "evid:dep-b"),
    )
    assert result.closed is False
    assert result.missing == ("evid:dep-c",)


def test_unresolved_dependencies_are_reported_as_a_stably_ordered_tuple() -> None:
    Closure = _closure()
    result = Closure(
        root_ids=("evid:release",),
        dependencies={
            "evid:release": ("evid:z", "evid:m"),
            "evid:m": ("evid:a",),
        },
        resolved=("evid:release",),
    )
    assert result.closed is False
    # Stable (sorted) order of the missing ids, not insertion order.
    assert result.missing == ("evid:a", "evid:m", "evid:z")


def test_closure_reaches_dependencies_of_dependencies() -> None:
    Closure = _closure()
    result = Closure(
        root_ids=("evid:integration",),
        dependencies={
            "evid:integration": ("evid:l1",),
            "evid:l1": ("evid:l2",),
            "evid:l2": ("evid:l3",),
        },
        # Only the deep dependency l3 is unresolved; the intermediates are resolved, so
        # closure is the only way l3's reachability is proven.
        resolved=("evid:integration", "evid:l1", "evid:l2"),
    )
    assert result.closed is False
    assert "evid:l3" in result.missing
    assert result.closure == ("evid:integration", "evid:l1", "evid:l2", "evid:l3")


def test_repeated_evaluation_is_pure_and_byte_identical() -> None:
    Closure = _closure()
    args = dict(
        root_ids=("evid:acceptance",),
        dependencies={"evid:acceptance": ("evid:b", "evid:a")},
        resolved=("evid:acceptance",),
    )
    first = Closure(**args)
    second = Closure(**args)
    assert first.closed == second.closed
    assert first.missing == second.missing
    assert first.closure == second.closure
