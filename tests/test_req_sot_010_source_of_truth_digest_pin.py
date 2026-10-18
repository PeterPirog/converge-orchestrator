from __future__ import annotations

from typing import Any

# Literal RED-lock marker: the pure guard that enforces REQ-SOT-010 does not exist
# yet, so every attempt to evaluate the authoritative Source-of-Truth digest pin fails
# with this exact marker until
# converge_orchestrator.config.evaluate_source_of_truth_pin is added.
_MARKER = "SOT010_SOURCE_OF_TRUTH_DIGEST_PIN_MISSING"

# Two fixed, distinct 64-hex-char SHA-256 content digests keep the pin / authoritative
# comparison in the tests deterministic and unambiguous.
_AUTHORITATIVE = "0123456789abcdef" * 4
_STALE_PIN = "fedcba9876543210" * 4


def _evaluate_pin(state: dict[str, Any]) -> tuple[str, ...]:
    """Evaluate the authoritative Source-of-Truth digest pin for ``state``.

    REQ-SOT-010: a run SHALL pin the authoritative Source-of-Truth content digest
    before repository mutation. ``converge_orchestrator.config.evaluate_source_of_truth_pin``
    is the pure, offline guard that enforces that contract. It does not exist yet, so
    this helper raises AssertionError with the literal marker
    ``SOT010_SOURCE_OF_TRUTH_DIGEST_PIN_MISSING`` (deterministic RED lock) instead of
    silently accepting a run that mutated the repository without a matching pinned
    digest.
    """
    try:
        from converge_orchestrator.config import evaluate_source_of_truth_pin
    except ImportError as exc:
        raise AssertionError(_MARKER) from exc
    return evaluate_source_of_truth_pin(state)


def test_matching_digest_pinned_before_mutation_is_accepted() -> None:
    # A digest matching the authoritative current Source-of-Truth content was pinned
    # before repository mutation: the guard is satisfied and reports no violation.
    state = {
        "pinned_source_of_truth_sha256": _AUTHORITATIVE,
        "current_source_of_truth_sha256": _AUTHORITATIVE,
        "repository_mutation_started": False,
    }
    assert _evaluate_pin(state) == ()


def test_missing_pin_before_mutation_fails_closed() -> None:
    # Repository mutation has begun but no Source-of-Truth content digest was pinned:
    # the guard must fail closed with the pin-missing clause and nothing else.
    state = {
        "pinned_source_of_truth_sha256": None,
        "current_source_of_truth_sha256": _AUTHORITATIVE,
        "repository_mutation_started": True,
    }
    assert _evaluate_pin(state) == ("source_of_truth_pin_missing_before_mutation",)


def test_non_matching_pin_fails_closed() -> None:
    # A pinned digest exists but differs from the authoritative current content
    # digest: the guard must fail closed with the not-authoritative clause and nothing
    # else.
    state = {
        "pinned_source_of_truth_sha256": _STALE_PIN,
        "current_source_of_truth_sha256": _AUTHORITATIVE,
        "repository_mutation_started": True,
    }
    assert _evaluate_pin(state) == ("source_of_truth_pin_not_authoritative",)
