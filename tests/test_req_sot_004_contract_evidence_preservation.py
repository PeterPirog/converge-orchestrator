"""RED tests for REQ-SOT-004: durable contract evidence preserves provenance.

REQ-SOT-004 requires the contract compiler to preserve each requirement's
identifier, statement, severity, source location, and the Source-of-Truth digest
in durable contract evidence. The preservation guard
``evaluate_contract_evidence_preservation`` and its ``write_contract``
enforcement do not exist yet, so the behaviour tests below fail with the literal
RED marker until the guard is implemented.
"""

import json
from pathlib import Path

import pytest

from converge_orchestrator.models import Contract, ContractSource, Requirement
from converge_orchestrator.spec import compile_contract, write_contract

try:
    from converge_orchestrator.spec import evaluate_contract_evidence_preservation
except ImportError:
    evaluate_contract_evidence_preservation = None

RED_MARKER = "SOT004_CONTRACT_EVIDENCE_PRESERVATION_MISSING"


def _guard() -> object:
    """Return the preservation guard, failing with the literal RED marker."""
    if evaluate_contract_evidence_preservation is None:
        pytest.fail(RED_MARKER)
    return evaluate_contract_evidence_preservation


def _full_contract() -> Contract:
    return Contract(
        source=ContractSource(path="architecture.md", sha256="abc123"),
        requirements=[
            Requirement(
                id="ARCH-1",
                statement="Domain must remain independent.",
                source="architecture.md:L2 [Architecture]",
                severity="mandatory",
            )
        ],
    )


def test_write_contract_rejects_blank_source_digest(tmp_path: Path) -> None:
    _guard()
    contract = _full_contract()
    contract.source.sha256 = ""
    evidence = tmp_path / "contract.json"
    with pytest.raises(ValueError):
        write_contract(evidence, contract)
    assert not evidence.exists()


def test_write_contract_rejects_blank_requirement_field(tmp_path: Path) -> None:
    _guard()
    contract = _full_contract()
    contract.requirements[0].statement = "   "
    evidence = tmp_path / "contract.json"
    with pytest.raises(ValueError):
        write_contract(evidence, contract)
    assert not evidence.exists()


def test_write_contract_preserves_full_provenance_round_trip(tmp_path: Path) -> None:
    spec = tmp_path / "architecture.md"
    spec.write_text(
        "# Architecture\nARCH-017 Domain must remain independent.\n",
        encoding="utf-8",
    )
    contract = compile_contract(spec)
    evidence = tmp_path / "contract.json"
    write_contract(evidence, contract)
    payload = json.loads(evidence.read_text(encoding="utf-8"))
    assert payload["source"]["sha256"] == contract.source.sha256
    record = payload["requirements"][0]
    assert record["id"] == "ARCH-017"
    assert record["statement"] == contract.requirements[0].statement
    assert record["severity"] == contract.requirements[0].severity
    assert record["source"] == contract.requirements[0].source
