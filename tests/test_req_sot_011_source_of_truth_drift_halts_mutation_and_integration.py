from __future__ import annotations

from typing import Any

# Literal RED-lock marker: the pure guard that enforces REQ-SOT-011 does not exist
# yet, so every attempt to evaluate authoritative Source-of-Truth digest drift fails
# with this exact marker until
# converge_orchestrator.config.evaluate_source_of_truth_drift is added.
_MARKER = "SOT011_SOURCE_OF_TRUTH_DRIFT_HALT_MISSING"

# Two fixed, distinct 64-hex-char SHA-256 content digests keep the drift comparison
# in the tests deterministic and unambiguous.
_AUTHORITATIVE = "0123456789abcdef" * 4
_DRIFTED_PIN = "fedcba9876543210" * 4


def _evaluate_drift(state: dict[str, Any]) -> tuple[str, ...]:
    """Evaluate authoritative Source-of-Truth digest drift for ``state``.

    REQ-SOT-011: a run SHALL stop mutation and integration when the authoritative
    Source-of-Truth content no longer matches its pinned digest.
    ``converge_orchestrator.config.evaluate_source_of_truth_drift`` is the pure,
    offline guard that enforces that contract. It does not exist yet, so this helper
    raises AssertionError with the literal marker
    ``SOT011_SOURCE_OF_TRUTH_DRIFT_HALT_MISSING`` (deterministic RED lock) instead of
    silently accepting a run that mutates the repository or integrates code while the
    authoritative Source-of-Truth content has drifted from the pinned digest.
    """
    try:
        from converge_orchestrator.config import evaluate_source_of_truth_drift
    except ImportError as exc:
        raise AssertionError(_MARKER) from exc
    return evaluate_source_of_truth_drift(state)


def test_drift_halts_mutation_and_integration() -> None:
    # A pinned digest that no longer equals the authoritative current digest is
    # drift: the guard must fail closed with both the stop-mutation and
    # stop-integration clauses, in fixed order (mutation first, then integration).
    state = {
        "pinned_source_of_truth_sha256": _DRIFTED_PIN,
        "current_source_of_truth_sha256": _AUTHORITATIVE,
    }
    assert _evaluate_drift(state) == (
        "source_of_truth_drift_halt_mutation",
        "source_of_truth_drift_halt_integration",
    )


def test_matching_digest_reports_no_drift() -> None:
    # A pinned digest that still equals the authoritative current digest is not
    # drift: the guard is satisfied and reports no halt clause.
    state = {
        "pinned_source_of_truth_sha256": _AUTHORITATIVE,
        "current_source_of_truth_sha256": _AUTHORITATIVE,
    }
    assert _evaluate_drift(state) == ()


def test_missing_pin_reports_no_drift_clause() -> None:
    # No Source-of-Truth digest was pinned: a missing pin is REQ-SOT-010's concern
    # (enforced by evaluate_source_of_truth_pin), not drift, so the drift guard
    # reports no drift clause.
    state = {
        "pinned_source_of_truth_sha256": None,
        "current_source_of_truth_sha256": _AUTHORITATIVE,
    }
    assert _evaluate_drift(state) == ()
