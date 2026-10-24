from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from converge_orchestrator.models import Requirement
from converge_orchestrator.spec import compile_contract

# Literal RED-lock marker: the pure preflight guard that enforces REQ-SOT-003 does not
# exist yet, so every attempt to evaluate explicit-identifier uniqueness fails with this
# exact marker until
# converge_orchestrator.spec.evaluate_explicit_identifier_uniqueness is added.
_MARKER = "SOT003_EXPLICIT_IDENTIFIER_UNIQUENESS_MISSING"


def _record(requirement_id: str, statement: str) -> Requirement:
    """Build a minimal authoritative normative record for the uniqueness guard."""
    return Requirement(
        id=requirement_id,
        statement=statement,
        source="spec.md:L1 [root]",
    )


def _evaluate(records: list[Requirement]) -> Any:
    """Evaluate explicit-identifier uniqueness for compiled normative records.

    REQ-SOT-003: every authoritative normative record SHALL contain a unique explicit
    identifier on the same physical line as its normative statement, and preflight SHALL
    reject duplicate explicit identifiers.
    ``converge_orchestrator.spec.evaluate_explicit_identifier_uniqueness`` is the pure,
    offline guard that enforces that contract. It does not exist yet, so this helper
    raises AssertionError with the literal marker
    ``SOT003_EXPLICIT_IDENTIFIER_UNIQUENESS_MISSING`` (deterministic RED lock) instead of
    silently accepting a contract whose records repeat the same explicit identifier.
    """
    try:
        from converge_orchestrator.spec import evaluate_explicit_identifier_uniqueness
    except ImportError as exc:
        raise AssertionError(_MARKER) from exc
    return evaluate_explicit_identifier_uniqueness(records)


def test_evaluate_accepts_unique_explicit_ids() -> None:
    # Two distinct explicit identifiers on two normative records: the guard is satisfied
    # and reports no clause.
    records = [
        _record("REQ-AAA-001", "REQ-AAA-001 The domain must not import infrastructure."),
        _record("REQ-BBB-002", "REQ-BBB-002 The service must be tested."),
    ]
    assert _evaluate(records) == ()


def test_evaluate_accepts_generated_ids() -> None:
    # Two generated stable identifiers (unique by construction) are never treated as
    # duplicate explicit identifiers, so the guard reports no clause.
    records = [
        _record("REQ-0A1B2C3D4E", "The domain must remain independent from adapters."),
        _record("REQ-5F6E7D8C9B", "The service must be tested."),
    ]
    assert _evaluate(records) == ()


def test_evaluate_reports_duplicate_explicit_identifier() -> None:
    # Two normative records declare the same explicit identifier: the guard must fail
    # closed with exactly the deterministic duplicate clause and nothing else.
    records = [
        _record("REQ-DUP-001", "REQ-DUP-001 The domain must not import infrastructure."),
        _record("REQ-DUP-001", "REQ-DUP-001 The service must be tested."),
    ]
    assert _evaluate(records) == ("duplicate_explicit_identifier:REQ-DUP-001",)


def test_compile_contract_rejects_duplicate_explicit_ids(tmp_path: Path) -> None:
    # Contract compilation is the preflight path: two normative lines carrying the same
    # explicit identifier must fail closed with a ValueError naming the duplicate ID.
    spec = tmp_path / "architecture.md"
    spec.write_text(
        "# Architecture\n"
        "REQ-DUP-001 The domain must not import infrastructure.\n"
        "REQ-DUP-001 The service must be tested.\n",
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="REQ-DUP-001"):
        compile_contract(spec)


def test_compile_contract_still_accepts_distinct_explicit_ids(tmp_path: Path) -> None:
    # Distinct explicit identifiers are still accepted and keep their line traceability.
    spec = tmp_path / "architecture.md"
    spec.write_text(
        "# Architecture\n"
        "REQ-AAA-001 The domain must not import infrastructure.\n"
        "REQ-BBB-002 The service must be tested.\n",
        encoding="utf-8",
    )
    contract = compile_contract(spec)
    assert [req.id for req in contract.requirements] == ["REQ-AAA-001", "REQ-BBB-002"]


def test_compile_contract_still_accepts_generated_ids(tmp_path: Path) -> None:
    # Normative lines without explicit identifiers still receive generated stable IDs
    # and compile unchanged.
    spec = tmp_path / "architecture.md"
    spec.write_text(
        "# Architecture\n"
        "The domain must remain independent from adapters.\n"
        "The service must be tested.\n",
        encoding="utf-8",
    )
    contract = compile_contract(spec)
    assert len(contract.requirements) == 2
    assert all(req.id.startswith("REQ-") for req in contract.requirements)
