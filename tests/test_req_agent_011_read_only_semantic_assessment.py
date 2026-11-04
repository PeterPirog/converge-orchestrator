"""RED lock test for REQ-AGENT-011: read-only canonical semantic compliance assessment.

REQ-AGENT-011 (CONVERGE_ORCHESTRATOR_2_0_SOURCE_OF_TRUTH.md:L259 [7. Deterministic
architecture and agent roles]): The system SHALL provide a read-only canonical compliance
assessment capability independent from the candidate writer for semantic requirements that
lack sufficient deterministic proof.

The codebase currently has no primitive that performs a deterministic, side-effect-free,
read-only compliance assessment for semantic (non-deterministically-verified) requirements.
These offline tests lock the not-yet-existing pure primitive
``converge_orchestrator.compliance.ComplianceEngine.assess_semantic_compliance``:

- against the old implementation the class has no such symbol, so every test fails with the
  RED marker defined by ``_MISSING`` in this module;
- once the primitive is added, the same tests must pass. It takes an immutable contract and
  the configured deterministic verifier map, identifies requirements that have no deterministic
  verifier proof, and returns a per-requirement compliance assessment built solely from those
  read-only inputs. It performs no filesystem writes, subprocess calls, git operations,
  model/provider invocations, or any writer-side mutations.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

import converge_orchestrator.compliance as compliance
from converge_orchestrator.models import (
    ComplianceSnapshot,
    Contract,
    ContractSource,
    QualityGate,
    Requirement,
    RequirementStatus,
)

_MISSING = "REQ-AGENT-011 READ-ONLY SEMANTIC COMPLIANCE ASSESSMENT MISSING"
_NAME = "assess_semantic_compliance"


def _primitive() -> Callable[..., Any]:
    """Resolve the REQ-AGENT-011 primitive, failing with the RED marker if it is absent."""
    method = getattr(compliance.ComplianceEngine, _NAME, None)
    assert method is not None, (
        f"{_MISSING}: converge_orchestrator.compliance.ComplianceEngine.{_NAME} is not defined; "
        "REQ-AGENT-011 requires a deterministic, read-only, in-memory semantic compliance "
        "assessment primitive that is independent of the Builder or any writer role and "
        "produces per-requirement results only from read-only contract and verifier inputs."
    )
    return method


def _contract() -> Contract:
    return Contract(
        source=ContractSource(path="architecture.md", sha256="abc"),
        requirements=[
            Requirement(
                id="REQ-AGENT-011-SEM-1",
                statement="A semantic requirement with no deterministic verifier.",
                source="architecture.md:L1",
            ),
            Requirement(
                id="REQ-AGENT-011-DET-1",
                statement="A requirement with a configured deterministic verifier.",
                source="architecture.md:L2",
            ),
            Requirement(
                id="REQ-AGENT-011-SEM-2",
                statement="Another semantic requirement with no deterministic verifier.",
                source="architecture.md:L3",
            ),
        ],
    )


def _verifiers() -> dict[str, list[QualityGate]]:
    return {
        "REQ-AGENT-011-DET-1": [
            QualityGate(name="deterministic-evidence", command=["true"])
        ],
    }


def test_semantic_assessment_primitive_exists() -> None:
    """The read-only semantic assessment symbol must exist on ComplianceEngine."""
    method = getattr(compliance.ComplianceEngine, _NAME, None)
    assert method is not None, _MISSING


def test_assessment_returns_canonical_snapshot_for_semantic_requirements() -> None:
    """A pure assessment returns UNVERIFIED entries only for requirements lacking verifier proof."""
    primitive = _primitive()
    contract = _contract()
    verifiers = _verifiers()

    before_contract = contract.model_dump()
    before_verifiers = {k: [g.model_dump() for g in v] for k, v in verifiers.items()}

    result = primitive(contract, verifiers)

    assert isinstance(result, ComplianceSnapshot), (
        f"{_NAME} must return a ComplianceSnapshot; got {type(result).__name__}"
    )

    semantic_ids = {"REQ-AGENT-011-SEM-1", "REQ-AGENT-011-SEM-2"}
    assert semantic_ids.issubset(set(result.entries)), (
        f"{_NAME} must assess every requirement that lacks deterministic verifier proof; "
        f"missing {semantic_ids - set(result.entries)}"
    )

    for requirement_id in semantic_ids:
        entry = result.entries[requirement_id]
        assert entry.status == RequirementStatus.UNVERIFIED, (
            f"semantic requirement {requirement_id} without deterministic proof must remain "
            f"UNVERIFIED, got {entry.status}"
        )
        assert entry.evidence, (
            f"semantic assessment for {requirement_id} must record explanatory evidence"
        )

    # The primitive must be read-only: inputs are unchanged.
    assert contract.model_dump() == before_contract
    assert {k: [g.model_dump() for g in v] for k, v in verifiers.items()} == before_verifiers


def test_assessment_never_calls_writer_methods(monkeypatch) -> None:
    """The assessment must be independent from any Builder or writer role."""
    calls: list[str] = []

    def _writer_guard(*args, **kwargs):
        calls.append("writer")
        raise AssertionError("semantic assessment must not invoke writer-side methods")

    monkeypatch.setattr(
        compliance.ComplianceEngine, "mark_local_pass", staticmethod(_writer_guard)
    )
    monkeypatch.setattr(
        compliance.ComplianceEngine, "mark_remote_verified", staticmethod(_writer_guard)
    )

    primitive = _primitive()
    result = primitive(_contract(), _verifiers())

    assert isinstance(result, ComplianceSnapshot)
    assert not calls, "semantic assessment must not depend on writer-side marking methods"
