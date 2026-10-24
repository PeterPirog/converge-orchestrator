from __future__ import annotations

from typing import Any

import pytest

# Literal RED-lock marker: the pure guard that enforces REQ-SOT-012 does not exist
# yet, so every attempt to evaluate mandatory-requirement convergence blocking fails
# with this exact marker until
# converge_orchestrator.config.evaluate_mandatory_convergence_blocking is added.
_MARKER = "SOT012_MANDATORY_CONVERGENCE_BLOCK_MISSING"

# Prefix of the deterministic clause emitted for each blocking mandatory record; the
# record's non-empty id is appended after a single colon.
_BLOCK = "mandatory_requirement_blocks_convergence"


def _evaluate(state: dict[str, Any]) -> tuple[str, ...]:
    """Evaluate mandatory-requirement convergence blocking for a run-state ``state``.

    REQ-SOT-012: mandatory requirement records SHALL block convergence until PASS.
    ``converge_orchestrator.config.evaluate_mandatory_convergence_blocking`` is the pure,
    offline guard that enforces that contract over a plain run-state mapping holding
    ``requirements`` (the stable-ordered list of requirement records, each a mapping with a
    non-empty ``id`` and an optional ``severity``) and ``compliance`` (a mapping of record
    id to a compliance-entry mapping carrying the record's status). It does not exist yet,
    so this helper raises AssertionError with the literal marker
    ``SOT012_MANDATORY_CONVERGENCE_BLOCK_MISSING`` (deterministic RED lock) instead of
    silently accepting convergence while a mandatory requirement record is not exactly PASS.
    """
    try:
        from converge_orchestrator.config import evaluate_mandatory_convergence_blocking
    except ImportError as exc:
        raise AssertionError(_MARKER) from exc
    return evaluate_mandatory_convergence_blocking(state)


def test_mandatory_record_not_pass_blocks_convergence() -> None:
    # A mandatory record whose compliance status is not exactly PASS must block
    # convergence with exactly one clause naming its id, while a PASS sibling does not.
    state = {
        "requirements": [
            {"id": "REQ-A", "severity": "mandatory"},
            {"id": "REQ-B", "severity": "mandatory"},
        ],
        "compliance": {
            "REQ-A": {"status": "pass"},
            "REQ-B": {"status": "fail"},
        },
    }
    assert _evaluate(state) == (f"{_BLOCK}:REQ-B",)


def test_mandatory_pass_and_recommended_records_do_not_block() -> None:
    # A mandatory record that is PASS and a recommended record (regardless of its status)
    # are both exempt, so the guard reports no blocking clause.
    state = {
        "requirements": [
            {"id": "REQ-A", "severity": "mandatory"},
            {"id": "REQ-B", "severity": "recommended"},
        ],
        "compliance": {
            "REQ-A": {"status": "pass"},
            "REQ-B": {"status": "fail"},
        },
    }
    assert _evaluate(state) == ()


def test_mandatory_record_with_missing_compliance_entry_blocks() -> None:
    # A mandatory record with no compliance entry has no PASS to rely on, so the guard
    # fails closed and blocks convergence for it (no convergence exemption).
    state = {
        "requirements": [
            {"id": "REQ-A", "severity": "mandatory"},
        ],
        "compliance": {},
    }
    assert _evaluate(state) == (f"{_BLOCK}:REQ-A",)


def test_absent_and_unknown_severity_are_mandatory_and_block() -> None:
    # A record with no severity and one with an unrecognised severity both fail closed to
    # mandatory, and block convergence while their status is not exactly PASS.
    state = {
        "requirements": [
            {"id": "REQ-A"},
            {"id": "REQ-B", "severity": "critical"},
        ],
        "compliance": {
            "REQ-A": {"status": "unverified"},
            "REQ-B": {"status": "partial"},
        },
    }
    assert _evaluate(state) == (
        f"{_BLOCK}:REQ-A",
        f"{_BLOCK}:REQ-B",
    )


def test_blocking_clauses_follow_stable_record_order() -> None:
    # Clauses must follow the requirement-record order (not be sorted by id), and a PASS
    # record contributes no clause even though it sits between two blocking records.
    state = {
        "requirements": [
            {"id": "REQ-3", "severity": "mandatory"},
            {"id": "REQ-1", "severity": "mandatory"},
            {"id": "REQ-2", "severity": "mandatory"},
        ],
        "compliance": {
            "REQ-3": {"status": "blocked"},
            "REQ-1": {"status": "pass"},
            "REQ-2": {"status": "unverified"},
        },
    }
    assert _evaluate(state) == (
        f"{_BLOCK}:REQ-3",
        f"{_BLOCK}:REQ-2",
    )


def test_missing_requirements_fails_closed() -> None:
    # A run-state without a requirements list is malformed: the guard must fail closed.
    with pytest.raises(ValueError):
        _evaluate({"compliance": {}})


def test_non_list_requirements_fail_closed() -> None:
    # A requirements value that is not a list is malformed: the guard must fail closed.
    with pytest.raises(ValueError):
        _evaluate({"requirements": "REQ-A", "compliance": {}})


def test_non_mapping_compliance_fails_closed() -> None:
    # A compliance value that is not a mapping is malformed: the guard must fail closed.
    with pytest.raises(ValueError):
        _evaluate(
            {
                "requirements": [{"id": "REQ-A", "severity": "mandatory"}],
                "compliance": ["REQ-A"],
            }
        )


def test_non_mapping_compliance_entry_fails_closed() -> None:
    # A compliance entry that is not a mapping is malformed: the guard must fail closed.
    with pytest.raises(ValueError):
        _evaluate(
            {
                "requirements": [{"id": "REQ-A", "severity": "mandatory"}],
                "compliance": {"REQ-A": "pass"},
            }
        )


def test_record_without_non_empty_id_fails_closed() -> None:
    # A requirement record without a non-empty id is malformed: the guard must fail closed.
    with pytest.raises(ValueError):
        _evaluate({"requirements": [{"id": ""}], "compliance": {}})
