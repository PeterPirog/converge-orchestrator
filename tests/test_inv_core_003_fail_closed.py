"""Lock INV-CORE-003: safety-relevant uncertainty must never be treated as PASS.

INV-CORE-003 (CONVERGE_ORCHESTRATOR_2_0_SOURCE_OF_TRUTH.md:L148 [4. Global invariants]):
Unknown, malformed, unavailable, ambiguous, or unverifiable safety-relevant state
SHALL NOT be treated as PASS. These pure deterministic tests pin the fail-closed
surfaces that enforce it: compliance promotion, external-acceptance CI policy,
final compliance, and review normalization. No network, subprocess, git, or models.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from converge_orchestrator.acceptance import _authoritative_ci_policy, _compliance_check
from converge_orchestrator.compliance import ComplianceEngine
from converge_orchestrator.models import (
    AgentResult,
    Contract,
    ContractSource,
    Requirement,
    RequirementStatus,
    RequirementVerification,
)
from converge_orchestrator.opencode import _aggregate_reviews, _normalize_review


def _contract() -> Contract:
    return Contract(
        source=ContractSource(path="CONVERGE_ORCHESTRATOR_2_0_SOURCE_OF_TRUTH.md", sha256="0" * 64),
        requirements=[
            Requirement(
                id="INV-CORE-003",
                statement="Safety-relevant uncertainty is never treated as PASS.",
                source="CONVERGE_ORCHESTRATOR_2_0_SOURCE_OF_TRUTH.md:L148",
            )
        ],
    )


def test_unverifiable_or_failed_verifications_never_promote_to_pass() -> None:
    """UNVERIFIED/FAIL/PARTIAL/BLOCKED evidence must never yield a PASS entry."""
    snapshot = ComplianceEngine.initial(_contract())
    for status in (
        RequirementStatus.UNVERIFIED,
        RequirementStatus.FAIL,
        RequirementStatus.PARTIAL,
        RequirementStatus.BLOCKED,
    ):
        result = ComplianceEngine.apply_verifications(
            snapshot, [RequirementVerification(requirement_id="INV-CORE-003", status=status)]
        )
        assert result.entries["INV-CORE-003"].status != RequirementStatus.PASS


def test_unknown_requirement_cannot_manufacture_pass_entry() -> None:
    """A result for an unknown requirement ID must not create PASS state."""
    unknown = RequirementVerification(requirement_id="UNKNOWN-999", status=RequirementStatus.PASS)
    result = ComplianceEngine.apply_verifications(ComplianceEngine.initial(_contract()), [unknown])
    assert "UNKNOWN-999" not in result.entries
    assert all(entry.status != RequirementStatus.PASS for entry in result.entries.values())


def test_malformed_verification_status_is_rejected_at_model_boundary() -> None:
    """Malformed status values cannot be constructed, let alone treated as PASS."""
    for bad_status in ("definitely-pass", None):
        with pytest.raises(ValidationError):
            RequirementVerification(
                requirement_id="INV-CORE-003", status=bad_status,  # type: ignore[arg-type]
            )


def test_verified_pass_evidence_still_promotes() -> None:
    """Positive control: the gate is fail-closed, not uniformly blocking."""
    snapshot = ComplianceEngine.initial(_contract())
    passed = RequirementVerification(requirement_id="INV-CORE-003", status=RequirementStatus.PASS)
    result = ComplianceEngine.apply_verifications(snapshot, [passed])
    assert result.entries["INV-CORE-003"].status == RequirementStatus.PASS


def _ci_policy(**overrides: object) -> dict:
    policy: dict = {
        "kind": "remote_policy",
        "authoritative": True,
        "source": "branch_protection",
        "required_checks": [{"context": "ci", "app_id": 1}],
    }
    policy.update(overrides)
    return policy


@pytest.mark.parametrize(
    "ci",
    [
        None,
        "pass",
        {},
        {"checks": "pass"},
        {"checks": []},
        {"checks": [_ci_policy(), _ci_policy()]},
        {"checks": [_ci_policy(required_checks="ci")]},
        {"checks": [_ci_policy(required_checks=[])]},
        {"checks": [_ci_policy(required_checks=[{"context": "   "}])]},
        {"checks": [_ci_policy(authoritative=False)]},
        {"checks": [_ci_policy(authoritative="true")]},
    ],
)
def test_ci_policy_fail_closed(ci: object) -> None:
    """Unavailable, malformed, ambiguous, or non-authoritative CI policy is never PASS."""
    ok, _required, _source = _authoritative_ci_policy(ci)
    assert ok is False


def _status(*, requirements: object = None, entries: object = None) -> dict:
    values: dict = {}
    if requirements is not None:
        values["requirements"] = requirements
    if entries is not None:
        values["compliance"] = {"entries": entries}
    return {"id": "run-1", "project_id": "proj-1", "status": "converged", "values": values}


def test_final_compliance_missing_or_bad_evidence_fails_closed() -> None:
    """Missing, unstructured, or requirement-free evidence must not be PASS."""
    mandatory = [{"id": "INV-CORE-003"}]
    cases = [
        {},
        {"values": "not-a-dict"},
        {"values": {}},
        _status(requirements=mandatory),
        _status(requirements=mandatory, entries={}),
        _status(requirements=mandatory, entries={"INV-CORE-003": "pass"}),
        _status(
            requirements=[{"id": "RECO-1", "severity": "recommended"}],
            entries={"RECO-1": {"status": "pass"}},
        ),
    ]
    for status in cases:
        check = _compliance_check(status)
        assert check.name == "final_compliance"
        assert check.ok is False


@pytest.mark.parametrize(
    "entry_status", ["unverified", "partial", "fail", "blocked", "pending", None]
)
def test_final_compliance_nonpass_mandatory_fails_closed(entry_status: object) -> None:
    """Any non-PASS mandatory requirement keeps the final gate closed."""
    entries = {"INV-CORE-003": {"status": entry_status}}
    check = _compliance_check(_status(requirements=[{"id": "INV-CORE-003"}], entries=entries))
    assert check.ok is False


def _agent(role: str, *, ok: bool, output: str = "", returncode: int = 0) -> AgentResult:
    return AgentResult(role=role, ok=ok, output=output, returncode=returncode)


def test_unavailable_review_result_is_rejected() -> None:
    """A failed/unavailable reviewer execution must become a reject verdict."""
    lane = "correctness_reviewer"
    review = _normalize_review(lane, _agent(lane, ok=False, output="crashed", returncode=1))
    assert review.verdict == "reject"
    assert review.reviewers == {lane: "reject"}


@pytest.mark.parametrize(
    "output",
    [
        "",
        "the reviewer approved this change",
        "[1, 2, 3]",
        '{"verdict": "maybe"}',
        '{"findings": []}',
        '{"verdict": null}',
        '{"verdict": "pass", "confidence": 5}',
    ],
)
def test_malformed_review_output_is_rejected(output: str) -> None:
    """Unparseable or structurally invalid reviewer output must become reject."""
    lane = "security_reviewer"
    review = _normalize_review(lane, _agent(lane, ok=True, output=output))
    assert review.verdict == "reject"
    assert review.reviewers == {lane: "reject"}


def test_review_aggregate_rejects_when_any_lane_is_not_pass() -> None:
    """Ambiguous mixed lanes (one pass, one unavailable) must aggregate to reject."""
    lanes = {
        "architecture_reviewer": _normalize_review(
            "architecture_reviewer",
            _agent("architecture_reviewer", ok=True, output='{"verdict": "pass"}'),
        ),
        "security_reviewer": _normalize_review(
            "security_reviewer",
            _agent("security_reviewer", ok=False, output="unavailable", returncode=3),
        ),
    }
    aggregate = _aggregate_reviews(list(lanes), lanes)
    assert aggregate.verdict == "reject"
    assert aggregate.reviewers == {"architecture_reviewer": "pass", "security_reviewer": "reject"}
