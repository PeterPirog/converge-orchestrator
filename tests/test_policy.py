from converge_orchestrator.models import (
    ComplianceSnapshot,
    GateResult,
    ReviewResult,
)
from converge_orchestrator.policy import DecisionKind, can_integrate


def _pass_gate() -> GateResult:
    return GateResult(name="tests", ok=True, required=True, returncode=0, output="")


def _decision(risk_flags: list[str]):
    return can_integrate(
        expected_spec_hash="abc",
        current_spec_hash="abc",
        gates=[_pass_gate()],
        review=ReviewResult(verdict="pass"),
        compliance=ComplianceSnapshot(),
        risk_flags=risk_flags,
    )


def test_policy_allows_only_green_evidence() -> None:
    assert _decision([]).kind == DecisionKind.ALLOW


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
    decision = _decision(["destructive_data_migration"])
    assert decision.kind == DecisionKind.INTERRUPT
    assert decision.reason == "HUMAN_RISK_POLICY"


def test_secret_material_is_blocking_not_human_approvable() -> None:
    decision = _decision(["secret_material_detected"])
    assert decision.kind == DecisionKind.BLOCK
    assert decision.reason == "RISK_POLICY_BLOCKED"


def test_blocking_risk_wins_over_human_only_risk() -> None:
    decision = _decision(["secret_required", "secret_material_detected"])
    assert decision.kind == DecisionKind.BLOCK
    assert decision.reason == "RISK_POLICY_BLOCKED"
