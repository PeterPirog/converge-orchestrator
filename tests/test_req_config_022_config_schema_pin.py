"""REQ-CONFIG-022: every run pins and records the authoritative
configuration-schema identity and unknown-field handling semantics before
mutation begins.

REQ-CONFIG-022 (CONVERGE_ORCHESTRATOR_2_0_SOURCE_OF_TRUTH.md:L220
[6. Project configuration and secret sources]): every run SHALL pin and record
the authoritative configuration-schema identity and unknown-field handling
semantics used to validate its effective project policy before mutation begins.

These offline, deterministic tests pin that contract against the pure,
fail-closed guard ``converge_orchestrator.config.evaluate_config_schema_pin``.
The guard takes a run-state mapping with the keys
``pinned_config_schema_identity``, ``recorded_config_schema_identity``, and
``unknown_field_handling_semantics``, and returns the violated clauses in the
fixed order:

* the guard returns an empty tuple when ``pinned_config_schema_identity`` and
  ``recorded_config_schema_identity`` are matching non-blank strings and
  ``unknown_field_handling_semantics`` is an explicit supported token:
  ``reject_unknown_authoritative_fields``, the REQ-CONFIG-020 behavior, or the
  stricter ``reject_all_unknown_fields``;
* the guard reports ``config_schema_identity_not_pinned_before_mutation`` when
  the pinned schema identity is missing, null, non-string, or blank, so
  repository mutation must not begin;
* the guard reports ``config_schema_identity_not_recorded`` when the recorded
  schema identity is missing, null, non-string, or blank;
* the guard reports ``config_schema_identity_pin_record_mismatch`` when both
  identities are non-blank strings but differ;
* the guard reports ``unknown_field_handling_semantics_not_recorded`` when the
  semantics value is missing, null, non-string, blank, or an unsupported token;
* the guard fails closed with ``ValueError`` on a non-mapping run state.

The guard does not exist yet, so every evaluation below fails with the literal
TDD RED evidence marker
``REQ-CONFIG-022-CONFIG-SCHEMA-PIN-GUARD-MISSING``
instead of silently proceeding to repository mutation without a pinned,
recorded, matching configuration-schema identity and explicit unknown-field
handling semantics. No filesystem, network, model, git, or subprocess access
occurs.
"""

from __future__ import annotations

from typing import Any

import pytest

# TDD RED evidence marker (tdd.expected_failure_pattern): carried by every
# failure below until the pure guard
# converge_orchestrator.config.evaluate_config_schema_pin is added. It is a
# literal, unique string that does not appear anywhere else in the repository,
# so its appearance in a test run is deterministic RED evidence.
_GUARD_MISSING = "REQ-CONFIG-022-CONFIG-SCHEMA-PIN-GUARD-MISSING"

# The explicit unknown-field handling semantics tokens the guard must accept.
_SUPPORTED_SEMANTICS = (
    "reject_unknown_authoritative_fields",
    "reject_all_unknown_fields",
)

# Sentinel marking a run-state key as absent (distinct from a null value).
_ABSENT = object()

_SCHEMA_IDENTITY = "converge-orchestrator-config/v1"


def _guard():
    """Return the guard, failing closed with the literal RED marker when absent."""
    try:
        from converge_orchestrator.config import evaluate_config_schema_pin
    except ImportError as exc:
        raise AssertionError(_GUARD_MISSING) from exc
    return evaluate_config_schema_pin


def _state(
    pinned: Any = _ABSENT,
    recorded: Any = _ABSENT,
    semantics: Any = _ABSENT,
) -> dict[str, Any]:
    """Build a run-state mapping from optional pin, record, and semantics values.

    The sentinel default omits a key entirely so the same helper can express a
    complete state as well as the missing / null / blank / mismatched shapes the
    clause tests exercise.
    """
    state: dict[str, Any] = {}
    if pinned is not _ABSENT:
        state["pinned_config_schema_identity"] = pinned
    if recorded is not _ABSENT:
        state["recorded_config_schema_identity"] = recorded
    if semantics is not _ABSENT:
        state["unknown_field_handling_semantics"] = semantics
    return state


def _evaluate(state: Any) -> tuple[str, ...]:
    """Evaluate the configuration-schema pin over a run-state snapshot."""
    return _guard()(state)


# --- Accepted state (no clause) -------------------------------------------------


@pytest.mark.parametrize("semantics", _SUPPORTED_SEMANTICS, ids=list(_SUPPORTED_SEMANTICS))
def test_supported_semantics_accepted(semantics: str) -> None:
    # A pinned identity matching the recorded identity, plus an explicit
    # supported unknown-field handling token: the guard reports no clause.
    state = _state(pinned=_SCHEMA_IDENTITY, recorded=_SCHEMA_IDENTITY, semantics=semantics)
    assert _evaluate(state) == ()


# --- Reported clauses (fixed order) ----------------------------------------------


def test_missing_pinned_identity_reports_not_pinned_clause() -> None:
    # Without a pinned schema identity there is no pin to validate against:
    # the guard reports exactly its clause.
    state = _state(
        recorded=_SCHEMA_IDENTITY,
        semantics="reject_unknown_authoritative_fields",
    )
    assert _evaluate(state) == ("config_schema_identity_not_pinned_before_mutation",)


def test_null_pinned_identity_reports_not_pinned_clause() -> None:
    # A null pinned schema identity is not a pin: the guard reports its clause.
    state = _state(
        pinned=None,
        recorded=_SCHEMA_IDENTITY,
        semantics="reject_unknown_authoritative_fields",
    )
    assert _evaluate(state) == ("config_schema_identity_not_pinned_before_mutation",)


def test_blank_pinned_identity_reports_not_pinned_clause() -> None:
    # A blank pinned schema identity is not a pin: the guard reports its clause.
    state = _state(
        pinned="   ",
        recorded=_SCHEMA_IDENTITY,
        semantics="reject_unknown_authoritative_fields",
    )
    assert _evaluate(state) == ("config_schema_identity_not_pinned_before_mutation",)


def test_missing_recorded_identity_reports_not_recorded_clause() -> None:
    # With a valid pin but no recorded schema identity: the guard reports
    # exactly the not-recorded clause.
    state = _state(
        pinned=_SCHEMA_IDENTITY,
        semantics="reject_unknown_authoritative_fields",
    )
    assert _evaluate(state) == ("config_schema_identity_not_recorded",)


def test_blank_recorded_identity_reports_not_recorded_clause() -> None:
    # A blank recorded schema identity is not a record: the guard reports its
    # clause.
    state = _state(
        pinned=_SCHEMA_IDENTITY,
        recorded="",
        semantics="reject_unknown_authoritative_fields",
    )
    assert _evaluate(state) == ("config_schema_identity_not_recorded",)


def test_mismatched_pin_and_record_report_mismatch_clause() -> None:
    # Both identities are non-blank strings but differ: the guard reports the
    # pin/record mismatch clause.
    state = _state(
        pinned=_SCHEMA_IDENTITY,
        recorded="converge-orchestrator-config/v2",
        semantics="reject_unknown_authoritative_fields",
    )
    assert _evaluate(state) == ("config_schema_identity_pin_record_mismatch",)


def test_missing_semantics_reports_not_recorded_clause() -> None:
    # Without an explicit unknown-field handling semantics token: the guard
    # reports exactly its clause.
    state = _state(pinned=_SCHEMA_IDENTITY, recorded=_SCHEMA_IDENTITY)
    assert _evaluate(state) == ("unknown_field_handling_semantics_not_recorded",)


def test_blank_semantics_reports_not_recorded_clause() -> None:
    # A blank semantics value is not an explicit declaration: the guard
    # reports its clause.
    state = _state(
        pinned=_SCHEMA_IDENTITY,
        recorded=_SCHEMA_IDENTITY,
        semantics="   ",
    )
    assert _evaluate(state) == ("unknown_field_handling_semantics_not_recorded",)


def test_unsupported_semantics_reports_not_recorded_clause() -> None:
    # A token outside the two explicit supported semantics is not an explicit
    # unknown-field handling declaration: the guard reports its clause.
    state = _state(
        pinned=_SCHEMA_IDENTITY,
        recorded=_SCHEMA_IDENTITY,
        semantics="allow_unknown_fields",
    )
    assert _evaluate(state) == ("unknown_field_handling_semantics_not_recorded",)


def test_all_missing_values_report_clauses_in_fixed_order() -> None:
    # With every declaration absent the guard reports the pin, record, and
    # semantics clauses in fixed order; no mismatch clause is reported because
    # a mismatch requires both identities to be non-blank.
    assert _evaluate({}) == (
        "config_schema_identity_not_pinned_before_mutation",
        "config_schema_identity_not_recorded",
        "unknown_field_handling_semantics_not_recorded",
    )


def test_mismatch_and_missing_semantics_report_fixed_order() -> None:
    # A pin/record mismatch plus an absent semantics declaration are reported
    # in fixed clause order (mismatch before semantics).
    state = _state(
        pinned=_SCHEMA_IDENTITY,
        recorded="converge-orchestrator-config/v2",
    )
    assert _evaluate(state) == (
        "config_schema_identity_pin_record_mismatch",
        "unknown_field_handling_semantics_not_recorded",
    )


# --- Fail-closed (ValueError) ----------------------------------------------------


@pytest.mark.parametrize(
    "state",
    ["pinned_config_schema_identity", 123, ["pinned_config_schema_identity"], None],
    ids=["string", "number", "list", "null"],
)
def test_non_mapping_run_state_fails_closed(state: Any) -> None:
    # A run state that is not a mapping cannot be assessed: the guard fails
    # closed with ValueError.
    with pytest.raises(ValueError):
        _evaluate(state)
