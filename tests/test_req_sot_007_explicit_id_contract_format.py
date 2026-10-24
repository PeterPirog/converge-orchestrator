"""RED tests for REQ-SOT-007: the explicit-ID single-line contract format is accepted.

REQ-SOT-007 requires Version 2.0 to continue to accept the explicit-ID single-line
normative contract format used by the Source of Truth without dependence on generated
requirement identifiers. The pure preflight guard
``converge_orchestrator.spec.evaluate_explicit_identifier_contract`` does not exist
yet, so the guard tests below fail with the literal RED marker until the guard and its
``compile_contract`` preflight wiring are added. The ``compile_contract`` tests
document the observable acceptance the guard protects and that must be preserved.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from converge_orchestrator.models import Requirement
from converge_orchestrator.spec import compile_contract

# Literal RED-lock marker: the pure preflight guard that enforces REQ-SOT-007 does not
# exist yet, so every attempt to evaluate explicit-ID single-line contract acceptance
# fails with this exact marker until
# converge_orchestrator.spec.evaluate_explicit_identifier_contract is added.
_MARKER = "SOT007_EXPLICIT_ID_SINGLE_LINE_CONTRACT_MISSING"


def _record(requirement_id: str, statement: str) -> Requirement:
    """Build a minimal authoritative normative record for the contract guard."""
    return Requirement(
        id=requirement_id,
        statement=statement,
        source="spec.md:L1 [root]",
    )


def _evaluate(records: list[Requirement]) -> Any:
    """Evaluate explicit-ID single-line contract acceptance for normative records.

    REQ-SOT-007: a normative record whose single source line declares an explicit
    identifier SHALL be assigned exactly that explicit identifier, never a generated
    one, so the Source-of-Truth explicit-ID single-line format compiles independently
    of generated identifiers.
    ``converge_orchestrator.spec.evaluate_explicit_identifier_contract`` is the pure,
    offline guard that enforces that contract. It does not exist yet, so this helper
    raises AssertionError with the literal marker
    ``SOT007_EXPLICIT_ID_SINGLE_LINE_CONTRACT_MISSING`` (deterministic RED lock) instead
    of silently accepting a record whose declared explicit ID was replaced by a
    generated one.
    """
    try:
        from converge_orchestrator.spec import evaluate_explicit_identifier_contract
    except ImportError as exc:
        raise AssertionError(_MARKER) from exc
    return evaluate_explicit_identifier_contract(records)


def test_evaluate_accepts_explicit_id_single_line_contract() -> None:
    # A single-line normative record whose source line declares a leading explicit ID
    # is assigned exactly that explicit identifier: the guard is satisfied and reports
    # no clause.
    records = [
        _record(
            "REQ-SOT-007",
            "REQ-SOT-007 Version 2.0 SHALL accept the explicit-ID single-line format.",
        )
    ]
    assert _evaluate(records) == ()


def test_evaluate_reports_generated_id_substitution() -> None:
    # A record whose single source line declares an explicit ID but was assigned a
    # generated identifier must fail closed with exactly one deterministic clause
    # naming the declared explicit ID and nothing else.
    records = [
        _record(
            "REQ-0A1B2C3D4E",
            "REQ-SOT-007 Version 2.0 SHALL accept the explicit-ID single-line format.",
        )
    ]
    assert _evaluate(records) == ("explicit_id_single_line_contract:REQ-SOT-007",)


def test_evaluate_reports_clauses_in_fixed_order() -> None:
    # Multiple records carrying substituted generated identifiers yield one
    # deterministic clause per declared explicit ID, in the fixed order the records
    # were given, while the compliant record contributes no clause.
    records = [
        _record("REQ-1111111111", "REQ-AAA-001 The domain must not import infrastructure."),
        _record("REQ-2222222222", "REQ-BBB-002 The service must be tested."),
        _record("REQ-CCC-003", "REQ-CCC-003 The API must validate input."),
    ]
    assert _evaluate(records) == (
        "explicit_id_single_line_contract:REQ-AAA-001",
        "explicit_id_single_line_contract:REQ-BBB-002",
    )


def test_compile_contract_keeps_explicit_id_single_line_format(tmp_path: Path) -> None:
    # The Source-of-Truth explicit-ID single-line format compiles to exactly the
    # declared explicit ID, with no generated identifier substituted in.
    spec = tmp_path / "architecture.md"
    spec.write_text(
        "# Source of Truth\n"
        "REQ-SOT-007 Version 2.0 SHALL accept the explicit-ID single-line format.\n",
        encoding="utf-8",
    )
    contract = compile_contract(spec)
    assert len(contract.requirements) == 1
    assert contract.requirements[0].id == "REQ-SOT-007"


def test_compile_contract_id_stable_across_statement_edits(tmp_path: Path) -> None:
    # Editing the statement text while keeping the same explicit ID yields the
    # identical requirement identifier, proving no dependence on generated IDs.
    first = tmp_path / "first.md"
    first.write_text(
        "# Source of Truth\n"
        "REQ-SOT-007 Version 2.0 SHALL accept the explicit-ID single-line format.\n",
        encoding="utf-8",
    )
    second = tmp_path / "second.md"
    second.write_text(
        "# Source of Truth\n"
        "REQ-SOT-007 Version 2.0 SHALL still accept the explicit-ID single-line format.\n",
        encoding="utf-8",
    )
    first_id = compile_contract(first).requirements[0].id
    second_id = compile_contract(second).requirements[0].id
    assert first_id == second_id == "REQ-SOT-007"


def test_compile_contract_still_accepts_distinct_and_generated_ids(tmp_path: Path) -> None:
    # Distinct explicit identifiers are still accepted and keep their IDs, while a
    # non-explicit normative line still receives a generated stable ID.
    spec = tmp_path / "architecture.md"
    spec.write_text(
        "# Architecture\n"
        "REQ-AAA-001 The domain must not import infrastructure.\n"
        "REQ-BBB-002 The service must be tested.\n"
        "The domain must remain independent from adapters.\n",
        encoding="utf-8",
    )
    contract = compile_contract(spec)
    ids = [record.id for record in contract.requirements]
    assert len(ids) == 3
    assert ids[0] == "REQ-AAA-001"
    assert ids[1] == "REQ-BBB-002"
    assert ids[2].startswith("REQ-")
    assert ids[2] not in ("REQ-AAA-001", "REQ-BBB-002")
