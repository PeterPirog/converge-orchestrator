"""RED tests for REQ-SOT-008: expose the compiled contract through a fail-closed preflight.

REQ-SOT-008 requires the Source-of-Truth preflight to expose the compiled contract and to
fail closed on explicit-ID substitution, duplicate explicit IDs, source digest/read-only
integrity, or severity ambiguity. The deterministic preflight
``converge_orchestrator.spec.preflight_contract`` does not exist yet, so the tests below
fail with the literal RED marker ``SOT008_CONTRACT_PREFLIGHT_MISSING`` until it returns the
compiled contract and rejects ambiguity; they pass once the preflight is added.

The tests are deterministic and offline: each one only reads/writes files under ``tmp_path``
and never touches the network, a model, git, or a subprocess.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from converge_orchestrator.models import Contract
from converge_orchestrator.spec import sha256_file

RED_MARKER = "SOT008_CONTRACT_PREFLIGHT_MISSING"

try:
    from converge_orchestrator.spec import preflight_contract
except ImportError:
    preflight_contract = None


def _preflight(path: Path, **kwargs: object) -> Contract:
    """Run the REQ-SOT-008 preflight, failing with the literal RED marker if it is missing.

    ``converge_orchestrator.spec.preflight_contract`` is the not-yet-existing deterministic
    preflight that exposes the compiled contract and fails closed on explicit-ID
    substitution, duplicate explicit IDs, source digest/read-only integrity, or severity
    ambiguity. It is the pure, offline guard that enforces REQ-SOT-008.
    """
    if preflight_contract is None:
        pytest.fail(RED_MARKER)
    return preflight_contract(path, **kwargs)


def test_preflight_exposes_compiled_contract_for_read_only_sot(tmp_path: Path) -> None:
    # A read-only, unambiguous Source of Truth exposes its compiled contract, preserving the
    # on-disk sha256 and every explicit requirement ID in order.
    spec = tmp_path / "architecture.md"
    spec.write_text(
        "# Architecture\n"
        "REQ-AAA-001 The domain must not import infrastructure.\n"
        "REQ-BBB-002 The service must be tested.\n",
        encoding="utf-8",
    )
    spec.chmod(0o444)
    contract = _preflight(spec)
    assert isinstance(contract, Contract)
    assert contract.source.sha256 == sha256_file(spec)
    assert [req.id for req in contract.requirements] == ["REQ-AAA-001", "REQ-BBB-002"]


def test_preflight_fails_closed_on_duplicate_explicit_ids(tmp_path: Path) -> None:
    # Two normative lines carrying the same explicit identifier fail closed with ValueError.
    spec = tmp_path / "architecture.md"
    spec.write_text(
        "# Architecture\n"
        "REQ-DUP-001 The domain must not import infrastructure.\n"
        "REQ-DUP-001 The service must be tested.\n",
        encoding="utf-8",
    )
    spec.chmod(0o444)
    with pytest.raises(ValueError):
        _preflight(spec)


def test_preflight_fails_closed_on_non_read_only_sot(tmp_path: Path) -> None:
    # A writable Source of Truth is not authoritative, so the preflight fails closed.
    spec = tmp_path / "architecture.md"
    spec.write_text(
        "# Architecture\n"
        "REQ-AAA-001 The domain must not import infrastructure.\n",
        encoding="utf-8",
    )
    spec.chmod(0o644)
    with pytest.raises(ValueError):
        _preflight(spec)


def test_preflight_reports_integrity_and_severity_clauses_in_fixed_order(tmp_path: Path) -> None:
    # A source-digest mismatch plus a severity-ambiguous record fail closed together, naming
    # the digest-integrity clause before the severity-ambiguity clause in a fixed order.
    spec = tmp_path / "architecture.md"
    spec.write_text(
        "# Architecture\n"
        "REQ-AMB-001 The domain must not import infrastructure and should prefer testing.\n",
        encoding="utf-8",
    )
    spec.chmod(0o444)
    with pytest.raises(ValueError) as excinfo:
        _preflight(spec, expected_sha256="0" * 64)
    message = str(excinfo.value)
    assert "contract_source_digest_mismatch" in message
    assert "severity_ambiguity:REQ-AMB-001" in message
    assert message.index("contract_source_digest_mismatch") < message.index(
        "severity_ambiguity:REQ-AMB-001"
    )
