"""RED tests for REQ-SOT-006: material Source-of-Truth changes require a new run.

REQ-SOT-006: material Source-of-Truth changes SHALL require a new run identity and a
new pinned digest rather than modifying the contract of an active run.
``converge_orchestrator.config.evaluate_source_of_truth_change_requires_new_run`` is
the pure, offline guard that enforces that contract over a run-state mapping. It does
not exist yet, so every evaluation below fails with the literal RED marker until the
guard is added.

The guard returns the violated clauses in fixed order (new run identity first, then
new pinned digest), reusing the same run-state keys the REQ-SOT-010/011 guards use for
the pinned / authoritative Source-of-Truth content digest. It performs no filesystem,
network, model, git, or subprocess access.
"""

from __future__ import annotations

from typing import Any

# Literal RED-lock marker: the pure guard that enforces REQ-SOT-006 does not exist yet,
# so every attempt to evaluate a material Source-of-Truth change fails with this exact
# marker until the guard is added.
_MARKER = "SOT006_SOURCE_OF_TRUTH_CHANGE_REQUIRES_NEW_RUN_MISSING"

# Two fixed, distinct 64-hex-char SHA-256 content digests keep the pinned /
# authoritative comparison in the tests deterministic and unambiguous.
_AUTHORITATIVE = "0123456789abcdef" * 4
_STALE_PIN = "fedcba9876543210" * 4

# Two fixed run identities distinguish the active run from a properly separated new run.
_ACTIVE_RUN = "run-active"
_NEW_RUN = "run-new"

# The two violated clauses, in the fixed order the guard must return them.
_CLAUSE_NEW_RUN = "source_of_truth_change_requires_new_run_identity"
_CLAUSE_NEW_PIN = "source_of_truth_change_requires_new_pinned_digest"

try:
    from converge_orchestrator.config import evaluate_source_of_truth_change_requires_new_run
except ImportError:
    evaluate_source_of_truth_change_requires_new_run = None  # type: ignore[assignment]


def _evaluate_change(state: dict[str, Any]) -> tuple[str, ...]:
    """Evaluate a Source-of-Truth change against REQ-SOT-006 for ``state``.

    REQ-SOT-006: a material Source-of-Truth change SHALL require a new run identity and
    a new pinned digest rather than modifying the contract of an active run.
    ``converge_orchestrator.config.evaluate_source_of_truth_change_requires_new_run`` is
    the pure, offline guard that enforces that contract. It does not exist yet, so this
    helper raises AssertionError with the literal marker
    ``SOT006_SOURCE_OF_TRUTH_CHANGE_REQUIRES_NEW_RUN_MISSING`` (deterministic RED lock)
    instead of silently accepting a material change applied under the active run or with
    a stale pin.
    """
    if evaluate_source_of_truth_change_requires_new_run is None:
        raise AssertionError(_MARKER)
    return evaluate_source_of_truth_change_requires_new_run(state)


def test_material_change_under_active_run_identity_requires_new_run() -> None:
    # A material Source-of-Truth change applied under the SAME (active) run identity
    # violates REQ-SOT-006: the run identity is not new. The pin is already updated to
    # the changed authoritative digest, isolating the new-run-identity clause.
    state = {
        "source_of_truth_change_is_material": True,
        "active_run_identity": _ACTIVE_RUN,
        "proposed_run_identity": _ACTIVE_RUN,
        "pinned_source_of_truth_sha256": _AUTHORITATIVE,
        "current_source_of_truth_sha256": _AUTHORITATIVE,
    }
    assert _evaluate_change(state) == (_CLAUSE_NEW_RUN,)


def test_material_change_under_new_run_with_stale_pin_requires_new_pin() -> None:
    # A material Source-of-Truth change under a NEW run identity is correctly separated
    # from the active run, but its pinned digest is stale (does not match the changed
    # authoritative digest): REQ-SOT-006 requires a new matching pin.
    state = {
        "source_of_truth_change_is_material": True,
        "active_run_identity": _ACTIVE_RUN,
        "proposed_run_identity": _NEW_RUN,
        "pinned_source_of_truth_sha256": _STALE_PIN,
        "current_source_of_truth_sha256": _AUTHORITATIVE,
    }
    assert _evaluate_change(state) == (_CLAUSE_NEW_PIN,)


def test_material_change_under_new_run_with_matching_pin_is_accepted() -> None:
    # A material Source-of-Truth change under a NEW run identity, with a pin equal to
    # the changed authoritative digest, is a properly separated new run: no violation.
    state = {
        "source_of_truth_change_is_material": True,
        "active_run_identity": _ACTIVE_RUN,
        "proposed_run_identity": _NEW_RUN,
        "pinned_source_of_truth_sha256": _AUTHORITATIVE,
        "current_source_of_truth_sha256": _AUTHORITATIVE,
    }
    assert _evaluate_change(state) == ()


def test_non_material_change_is_accepted() -> None:
    # A non-material Source-of-Truth change does not require a new run identity or a new
    # pinned digest: the guard reports no clause, even under the active run with a stale
    # pin.
    state = {
        "source_of_truth_change_is_material": False,
        "active_run_identity": _ACTIVE_RUN,
        "proposed_run_identity": _ACTIVE_RUN,
        "pinned_source_of_truth_sha256": _STALE_PIN,
        "current_source_of_truth_sha256": _AUTHORITATIVE,
    }
    assert _evaluate_change(state) == ()


def test_material_change_reports_clauses_in_fixed_order() -> None:
    # A material Source-of-Truth change applied under the active run identity with a
    # stale pin violates both clauses: the guard must report the new-run-identity clause
    # first, then the new-pinned-digest clause, in that fixed order.
    state = {
        "source_of_truth_change_is_material": True,
        "active_run_identity": _ACTIVE_RUN,
        "proposed_run_identity": _ACTIVE_RUN,
        "pinned_source_of_truth_sha256": _STALE_PIN,
        "current_source_of_truth_sha256": _AUTHORITATIVE,
    }
    assert _evaluate_change(state) == (_CLAUSE_NEW_RUN, _CLAUSE_NEW_PIN)
