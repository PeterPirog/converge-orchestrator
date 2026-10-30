"""REQ-DATA-002: provider retry or fallback SHALL NOT widen authorized data egress.

REQ-DATA-002 (CONVERGE_ORCHESTRATOR_2_0_SOURCE_OF_TRUTH.md:L231
[6. Project configuration and secret sources]): Provider retry or fallback SHALL
NOT widen authorized data egress.

These offline, deterministic tests pin the fail-closed guard
``converge_orchestrator.config.evaluate_retry_fallback_egress``:

* the guard returns an empty tuple when every retry/fallback attempt targets the
  primary attempt's endpoint category or a category whose
  ``repository_data_egress`` is explicitly ``false``;
* the guard reports ``retry_or_fallback_widens_authorized_data_egress:<category>``
  in attempt order for each attempt that targets a different category whose
  ``repository_data_egress`` is explicitly ``true``;
* the guard fails closed with ``ValueError`` on a missing or invalid
  ``provider_trust`` mapping, an unknown primary endpoint category, or an unknown
  or non-string attempt category.

The guard does not exist yet, so every evaluation below fails with the literal
TDD RED evidence marker ``REQ-DATA-002-RETRY-FALLBACK-EGRESS-GUARD-MISSING``
instead of silently accepting a retry or fallback that would transmit
repository-derived data outside the primary attempt's authorized egress
category. No network, model, MCP, provider, subprocess, git, or filesystem
access occurs.
"""

from __future__ import annotations

from typing import Any

import pytest

# TDD RED evidence marker (tdd.expected_failure_pattern): carried by every failure
# below until the pure guard converge_orchestrator.config.evaluate_retry_fallback_egress
# is added. It is a literal, unique string that does not appear anywhere else in the
# repository, so its appearance in a test run is deterministic RED evidence.
_GUARD_MISSING = "REQ-DATA-002-RETRY-FALLBACK-EGRESS-GUARD-MISSING"

# The four endpoint categories in the fixed order the requirement names them
# (model, gateway, mcp, external analysis).
_CATEGORIES = ("model", "gateway", "mcp", "external_analysis")

# A declared trust class that is neither local nor trusted; the guard does not
# need to re-report trust-class violations (REQ-CONFIG-014 covers them), but the
# egress-widening behavior must still be evaluated against the declared value.
_UNTRUSTED_CLASS = "untrusted"


def _guard():
    """Return the guard, failing closed with the literal RED marker when absent."""
    try:
        from converge_orchestrator.config import evaluate_retry_fallback_egress
    except ImportError as exc:
        raise AssertionError(_GUARD_MISSING) from exc
    return evaluate_retry_fallback_egress


def _evaluate(
    provider_trust: dict[str, Any] | None,
    primary_category: str,
    attempts: list[str] | tuple[str, ...],
) -> tuple[str, ...]:
    """Evaluate retry/fallback egress for the given primary and attempt categories."""
    return _guard()(provider_trust, primary_category, attempts)


def _declared(trust_class: str, egress: bool) -> dict[str, Any]:
    """One fully-declared endpoint category with an explicit trust class and egress."""
    return {"trust_class": trust_class, "repository_data_egress": egress}


def _complete_provider_trust() -> dict[str, Any]:
    """A complete provider_trust mapping: all four categories declared."""
    return {
        "model": _declared("local", True),
        "gateway": _declared("trusted", True),
        "mcp": _declared("local", False),
        "external_analysis": _declared("trusted", False),
    }


def test_retry_to_primary_category_is_accepted() -> None:
    # A retry that targets the same endpoint category as the primary attempt
    # cannot widen egress, even when that category permits repository-data egress.
    assert _evaluate(_complete_provider_trust(), "model", ["model", "model"]) == ()


def test_fallback_to_primary_category_is_accepted() -> None:
    # A fallback that targets the same endpoint category as the primary attempt
    # is authorized within the primary egress category.
    assert _evaluate(_complete_provider_trust(), "gateway", ("gateway",)) == ()


def test_empty_attempt_list_is_accepted() -> None:
    # No retry or fallback attempts means no egress widening is possible.
    assert _evaluate(_complete_provider_trust(), "model", []) == ()


def test_attempt_to_category_with_egress_false_is_accepted() -> None:
    # A fallback to a different category that does not permit repository-data
    # egress cannot widen authorized data egress.
    assert _evaluate(_complete_provider_trust(), "model", ["mcp"]) == ()


def test_attempt_to_same_category_with_egress_false_is_accepted() -> None:
    # When the primary category itself forbids repository-data egress, retrying
    # to the same category stays within the authorized (empty) egress envelope.
    provider_trust = _complete_provider_trust()
    provider_trust["model"] = _declared("local", False)
    assert _evaluate(provider_trust, "model", ["model"]) == ()


def test_attempt_to_different_category_with_egress_true_widens_egress() -> None:
    # A fallback from model to gateway targets a different category that also
    # permits repository-data egress, widening the authorized egress envelope.
    assert _evaluate(_complete_provider_trust(), "model", ["gateway"]) == (
        "retry_or_fallback_widens_authorized_data_egress:gateway",
    )


def test_attempt_to_untrusted_category_with_egress_true_widens_egress() -> None:
    # Widening is reported from the attempt category's declared egress value,
    # independent of whether its trust class is local/trusted.
    provider_trust = _complete_provider_trust()
    provider_trust["external_analysis"] = _declared(_UNTRUSTED_CLASS, True)
    assert _evaluate(provider_trust, "model", ["external_analysis"]) == (
        "retry_or_fallback_widens_authorized_data_egress:external_analysis",
    )


def test_widening_attempts_reported_in_attempt_order() -> None:
    # Each widening attempt is reported in the order it appears, with the
    # primary category's own egress value playing no role in same-category
    # retries.
    provider_trust = _complete_provider_trust()
    provider_trust["external_analysis"] = _declared("trusted", True)
    assert _evaluate(
        provider_trust,
        "model",
        ["gateway", "model", "external_analysis", "mcp"],
    ) == (
        "retry_or_fallback_widens_authorized_data_egress:gateway",
        "retry_or_fallback_widens_authorized_data_egress:external_analysis",
    )


def test_missing_provider_trust_fails_closed() -> None:
    # The provider_trust mapping is required to know each category's authorized
    # egress; a missing mapping must fail closed.
    with pytest.raises(ValueError):
        _evaluate(None, "model", ["model"])


def test_empty_provider_trust_fails_closed() -> None:
    # An empty provider_trust mapping cannot declare any endpoint categories:
    # the guard must fail closed rather than silently accepting the attempts.
    with pytest.raises(ValueError):
        _evaluate({}, "model", ["model"])


def test_incomplete_provider_trust_fails_closed() -> None:
    # A provider_trust mapping missing a required endpoint category cannot
    # validate the egress envelope; the guard must fail closed.
    provider_trust = _complete_provider_trust()
    del provider_trust["mcp"]
    with pytest.raises(ValueError):
        _evaluate(provider_trust, "model", ["model"])


def test_unknown_primary_category_fails_closed() -> None:
    # The primary attempt declares an endpoint category outside the four known
    # categories, so its authorized egress envelope cannot be determined.
    with pytest.raises(ValueError):
        _evaluate(_complete_provider_trust(), "custom_endpoint", ["model"])


def test_unknown_attempt_category_fails_closed() -> None:
    # A retry or fallback target is an unknown endpoint category: the guard
    # must fail closed rather than ignoring the attempt.
    with pytest.raises(ValueError):
        _evaluate(_complete_provider_trust(), "model", ["custom_endpoint"])


@pytest.mark.parametrize("bad_attempt", [None, 123, ""], ids=["none", "int", "blank"])
def test_non_string_attempt_category_fails_closed(bad_attempt: Any) -> None:
    # An attempt category that is not a non-empty string is malformed and must
    # fail closed rather than being treated as a known category.
    with pytest.raises(ValueError):
        _evaluate(_complete_provider_trust(), "model", [bad_attempt])
