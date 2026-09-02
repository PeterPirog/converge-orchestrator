from converge_orchestrator.models import (
    ComplianceSnapshot,
    GateResult,
    ReviewResult,
)
from converge_orchestrator.policy import DecisionKind, can_integrate


def _pass_gate() -> GateResult:
    return GateResult(name="tests", ok=True, required=True, returncode=0, output="")


def test_policy_allows_only_green_evidence() -> None:
    decision = can_integrate(
        expected_spec_hash="abc",
        current_spec_hash="abc",
        gates=[_pass_gate()],
        review=ReviewResult(verdict="pass"),
        compliance=ComplianceSnapshot(),
        risk_flags=[],
    )
    assert decision.kind == DecisionKind.ALLOW


def test_policy_blocks_spec_drift() -> None:
    decision = can_integrate(
        expected_spec_hash="abc",
        current_spec_hash="changed",
        gates=[_pass_gate()],
        review=ReviewResult(verdict="pass"),
        compliance=ComplianceSnapshot(),
        risk_flags=[],
    )
    assert decision.kind == DecisionKind.BLOCK
    assert decision.reason == "SPEC_CHANGED"


def test_policy_interrupts_human_only_risk() -> None:
    decision = can_integrate(
        expected_spec_hash="abc",
        current_spec_hash="abc",
        gates=[_pass_gate()],
        review=ReviewResult(verdict="pass"),
        compliance=ComplianceSnapshot(),
        risk_flags=["destructive_data_migration"],
    )
    assert decision.kind == DecisionKind.INTERRUPT
