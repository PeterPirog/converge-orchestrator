"""RED tests for REQ-SOT-009: reject fallback and non-explicit requirements in preflight.

REQ-SOT-009 (CONVERGE_ORCHESTRATOR_2_0_SOURCE_OF_TRUTH.md:L195, [5. Source-of-Truth
contract]) requires the production Source-of-Truth preflight to reject a contract that
contains no recognized normative records, relies on compiler fallback extraction, or yields
any compiled requirement without a unique explicit stable identifier from the authoritative
Markdown source, while still returning the compiled Contract for a fully explicit
read-only source.

``converge_orchestrator.spec.preflight_contract`` currently accepts fallback-compiled and
generated-ID contracts, so the rejection tests below fail with the literal RED marker
``SOT009_NORMATIVE_IDENTITY_PREFLIGHT_MISSING`` until the fail-closed normative-identity
guards are added; they pass once ``preflight_contract`` fails closed on those clauses. The
fully-explicit source test documents the acceptance that MUST be preserved, together with
``compile_contract``'s generated-ID acceptance relied on by REQ-SOT-003 and REQ-SOT-007.

The tests are deterministic and offline: each one only reads/writes files under ``tmp_path``
and never touches the network, a model, git, or a subprocess.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from converge_orchestrator.models import Contract
from converge_orchestrator.spec import preflight_contract, sha256_file

# Literal RED-lock marker: the fail-closed normative-identity guards that enforce
# REQ-SOT-009 are not yet wired into the production preflight, so any attempt to rely on
# them fails with this exact marker until
# converge_orchestrator.spec.preflight_contract rejects the corresponding clause.
RED_MARKER = "SOT009_NORMATIVE_IDENTITY_PREFLIGHT_MISSING"


def _make_read_only_sot(tmp_path: Path, body: str) -> Path:
    """Write an authoritative read-only Source-of-Truth Markdown file under ``tmp_path``."""
    spec = tmp_path / "architecture.md"
    spec.write_text(body, encoding="utf-8")
    spec.chmod(0o444)
    return spec


def _preflight_rejects(path: Path, expected_clauses: tuple[str, ...]) -> str:
    """Return the preflight failure message when it fails closed naming every expected clause.

    ``converge_orchestrator.spec.preflight_contract`` is the deterministic, offline guard
    that enforces REQ-SOT-009. It is not yet wired with the fail-closed normative-identity
    guards, so a source whose records are unrecognised, fallback-extracted, or carry no
    explicit stable identifier is silently accepted instead of failing closed. This helper
    raises the literal marker ``SOT009_NORMATIVE_IDENTITY_PREFLIGHT_MISSING`` (deterministic
    RED lock) in that case instead of silently accepting a contract that violates
    REQ-SOT-009; when the guards are present it returns the rejection message so callers can
    assert clause ordering and identifier shape.
    """
    try:
        preflight_contract(path)
    except ValueError as exc:
        message = str(exc)
        if all(clause in message for clause in expected_clauses):
            return message
    pytest.fail(RED_MARKER)


def test_preflight_rejects_source_with_no_recognized_normative_records(tmp_path: Path) -> None:
    # A read-only Source of Truth with no recognized normative records contains no
    # authoritative requirement, so the preflight MUST fail closed naming the no-records
    # clause.
    spec = _make_read_only_sot(
        tmp_path,
        "# Architecture\n"
        "Service notes only.\n",
    )
    _preflight_rejects(spec, ("contract_no_recognized_normative_records",))


def test_preflight_rejects_compiler_fallback_extraction(tmp_path: Path) -> None:
    # A read-only Source of Truth whose only records are produced by the compiler fallback
    # (a long non-normative line) has no recognized normative records and relies on
    # fallback extraction, so the preflight MUST fail closed naming both clauses, with the
    # no-records clause before the fallback clause in a fixed order.
    spec = _make_read_only_sot(
        tmp_path,
        "# Architecture\n"
        "The pipeline description spans a long line without normative language.\n",
    )
    message = _preflight_rejects(
        spec,
        (
            "contract_no_recognized_normative_records",
            "contract_relies_on_compiler_fallback_extraction",
        ),
    )
    assert message.index("contract_no_recognized_normative_records") < message.index(
        "contract_relies_on_compiler_fallback_extraction"
    )


def test_preflight_rejects_requirement_without_explicit_stable_identifier(tmp_path: Path) -> None:
    # A read-only Source of Truth whose single normative record carries no explicit stable
    # identifier (and so receives a generated one) MUST fail closed naming the requirement
    # by that generated stable identifier.
    spec = _make_read_only_sot(
        tmp_path,
        "# Architecture\n"
        "The domain must remain independent from adapters.\n",
    )
    message = _preflight_rejects(
        spec,
        ("requirement_without_explicit_stable_identifier:",),
    )
    assert re.search(r"requirement_without_explicit_stable_identifier:REQ-[0-9A-F]{10}", message)


def test_preflight_still_returns_contract_for_fully_explicit_read_only_sot(tmp_path: Path) -> None:
    # A read-only Source of Truth whose every normative line declares a unique explicit
    # identifier still exposes its compiled contract, preserving REQ-SOT-003 / REQ-SOT-007
    # acceptance, its on-disk sha256, and every explicit requirement ID in order.
    spec = _make_read_only_sot(
        tmp_path,
        "# Architecture\n"
        "REQ-AAA-001 The domain must not import infrastructure.\n"
        "REQ-BBB-002 The service must be tested.\n",
    )
    contract = preflight_contract(spec)
    assert isinstance(contract, Contract)
    assert contract.source.sha256 == sha256_file(spec)
    assert [req.id for req in contract.requirements] == ["REQ-AAA-001", "REQ-BBB-002"]
