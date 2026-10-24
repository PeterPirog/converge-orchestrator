"""RED tests for REQ-SOT-005: deterministic, order-independent severity classification.

REQ-SOT-005 requires requirement severity classification to be deterministic. The
current compiler downgrades any statement that also contains an advisory word
(``should``) to ``recommended`` even when a mandatory normative keyword (``must``) is
present, so a mandatory requirement is silently dropped from targeting. The pure,
order-independent classifier ``classify_requirement_severity`` does not exist yet, so
the tests below fail with the literal RED marker until mandatory-keyword precedence is
implemented; they pass once classification is deterministic.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from converge_orchestrator.spec import compile_contract

try:
    from converge_orchestrator.spec import classify_requirement_severity
except ImportError:
    classify_requirement_severity = None

RED_MARKER = "SOT005_SEVERITY_CLASSIFICATION_NOT_DETERMINISTIC"


def _classify(statement: str) -> str:
    """Return the pure severity classifier, failing with the literal RED marker.

    ``converge_orchestrator.spec.classify_requirement_severity`` is the not-yet-
    existing pure, order-independent classifier that enforces REQ-SOT-005: any
    mandatory normative keyword classifies a statement as ``mandatory`` even when an
    advisory word is also present, and only advisory-only statements classify as
    ``recommended``.
    """
    if classify_requirement_severity is None:
        pytest.fail(RED_MARKER)
    return classify_requirement_severity(statement)


def test_mandatory_only_is_mandatory() -> None:
    assert _classify("The orchestrator must preserve provenance.") == "mandatory"


def test_advisory_only_is_recommended() -> None:
    assert _classify("The orchestrator should prefer explicit quality gates.") == "recommended"


def test_mixed_must_then_should_is_mandatory() -> None:
    assert _classify("The orchestrator must preserve provenance and should log it.") == "mandatory"


def test_mixed_should_then_must_is_mandatory() -> None:
    assert _classify("The orchestrator should log provenance and must preserve it.") == "mandatory"


def test_classification_is_deterministic() -> None:
    statement = "The orchestrator must preserve provenance and should log it."
    assert _classify(statement) == _classify(statement) == "mandatory"


def test_compile_contract_mixed_line_is_mandatory(tmp_path: Path) -> None:
    spec = tmp_path / "architecture.md"
    spec.write_text(
        "# Architecture\n"
        "The orchestrator must preserve provenance and should log it.\n",
        encoding="utf-8",
    )
    contract = compile_contract(spec)
    assert len(contract.requirements) == 1
    assert contract.requirements[0].severity == "mandatory"


def test_compile_contract_advisory_only_line_is_recommended(tmp_path: Path) -> None:
    spec = tmp_path / "architecture.md"
    spec.write_text(
        "# Architecture\n"
        "The orchestrator should prefer explicit quality gates.\n",
        encoding="utf-8",
    )
    contract = compile_contract(spec)
    assert len(contract.requirements) == 1
    assert contract.requirements[0].severity == "recommended"
