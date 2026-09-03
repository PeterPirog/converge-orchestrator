from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from .models import ComplianceSnapshot, GateResult, ReviewResult
from .quality import required_gates_pass


class DecisionKind(StrEnum):
    ALLOW = "allow"
    BLOCK = "block"
    INTERRUPT = "interrupt"


@dataclass(frozen=True)
class Decision:
    kind: DecisionKind
    reason: str


BLOCKING_RISK_FLAGS = {
    "secret_material_detected",
}

HUMAN_ONLY_FLAGS = {
    "contradictory_requirements",
    "destructive_data_migration",
    "forbidden_public_api_change",
    "secret_required",
    "critical_auth_redesign",
}


def can_integrate(
    *,
    expected_spec_hash: str,
    current_spec_hash: str,
    gates: list[GateResult],
    review: ReviewResult,
    compliance: ComplianceSnapshot,
    risk_flags: list[str],
) -> Decision:
    if expected_spec_hash != current_spec_hash:
        return Decision(DecisionKind.BLOCK, "SPEC_CHANGED")
    if not required_gates_pass(gates):
        return Decision(DecisionKind.BLOCK, "QUALITY_GATE_FAILED")
    if BLOCKING_RISK_FLAGS.intersection(risk_flags):
        return Decision(DecisionKind.BLOCK, "RISK_POLICY_BLOCKED")
    if review.verdict != "pass":
        return Decision(DecisionKind.BLOCK, "REVIEW_REJECTED")
    if compliance.mandatory_regressions > 0:
        return Decision(DecisionKind.BLOCK, "ARCHITECTURE_REGRESSION")
    if HUMAN_ONLY_FLAGS.intersection(risk_flags):
        return Decision(DecisionKind.INTERRUPT, "HUMAN_RISK_POLICY")
    return Decision(DecisionKind.ALLOW, "ALL_GATES_PASS")
