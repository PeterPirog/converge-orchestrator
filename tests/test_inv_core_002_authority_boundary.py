"""Lock the INV-CORE-002 authority boundary with deterministic regression tests.

INV-CORE-002: LangGraph plus deterministic Converge policy own workflow routing,
durable continuation, target selection, gate interpretation, compliance transitions
and integration authorization; model output must not directly exercise those
authorities. These tests use only pure policy/routing functions (no models, network,
or subprocesses) and prove parsed planner/model output cannot set workflow
authority state, self-route past the deterministic TDD gate, or use a model review
verdict to authorize integration past failed gates.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

from converge_orchestrator.compliance import ComplianceEngine
from converge_orchestrator.graph import route_after_tdd_red
from converge_orchestrator.models import (
    ComplianceSnapshot,
    Contract,
    ContractSource,
    GateResult,
    ProjectConfig,
    Requirement,
    RequirementStatus,
    ReviewResult,
    TaskEnvelope,
)
from converge_orchestrator.policy import DecisionKind, can_integrate


def _pass_gate() -> GateResult:
    return GateResult(name="tests", ok=True, required=True, returncode=0, output="")


def _fail_gate() -> GateResult:
    return GateResult(name="tests", ok=False, required=True, returncode=1, output="boom")


def test_model_review_verdict_cannot_authorize_integration_past_failed_gates() -> None:
    """A model review 'pass' verdict must not authorize integration past a failed gate."""
    for verdict in ("pass", "reject"):
        decision = can_integrate(
            expected_spec_hash="abc",
            current_spec_hash="abc",
            gates=[_fail_gate()],
            review=ReviewResult(verdict=verdict),  # type: ignore[arg-type]
            compliance=ComplianceSnapshot(),
            risk_flags=[],
        )
        assert decision.kind == DecisionKind.BLOCK
        assert decision.reason == "QUALITY_GATE_FAILED"

    # With a passing gate the model verdict is the only remaining gate, so it is
    # still respected -- a subordinate input, never an override of the gate.
    allow = can_integrate(
        expected_spec_hash="abc",
        current_spec_hash="abc",
        gates=[_pass_gate()],
        review=ReviewResult(verdict="pass"),
        compliance=ComplianceSnapshot(),
        risk_flags=[],
    )
    assert allow.kind == DecisionKind.ALLOW
    assert allow.reason == "ALL_GATES_PASS"


def test_deterministic_tdd_gate_owns_routing_past_red_not_model_output(tmp_path: Path) -> None:
    """Routing past the deterministic TDD gate is owned by the gate result, not the model."""
    requirements = tmp_path / "requirements.md"
    requirements.write_text("INV-CORE-002 must hold.\n", encoding="utf-8")
    cfg = ProjectConfig(
        repo_path=tmp_path,
        requirements_path=requirements,
        require_spec_read_only=False,
        agents={},
    )
    task = TaskEnvelope(
        id="INV-CORE-002-1",
        requirement_ids=["INV-CORE-002"],
        title="Lock authority boundary",
        objective="Prove model output cannot exercise workflow authority",
        allowed_paths=["tests/**"],
        change_kind="behavior",
        tdd={
            "mode": "required",
            "test_paths": ["tests/**"],
            "test_gate": "unit-test",
            "expected_failure_pattern": "AUTHORITY_DRIFT",
            "rationale": "Behavior change requires a failing test first.",
        },
    )

    def state(red_ok: bool, review_verdict: str) -> dict:
        return {
            "config_path": str(tmp_path / "converge.yaml"),
            "task": task.model_dump(mode="json"),
            "tdd_red_attempts": 0,
            # A model review verdict lives in state but must not influence routing.
            "review_result": ReviewResult(verdict=review_verdict).model_dump(mode="json"),
            "tdd_red_result": GateResult(
                name="tdd_red",
                ok=red_ok,
                required=True,
                returncode=0 if red_ok else 1,
                output="{}",
            ).model_dump(mode="json"),
        }

    with patch("converge_orchestrator.graph.load_config", return_value=cfg):
        # Failed RED gate -> never routes to build, whatever the model claims.
        assert route_after_tdd_red(state(False, "pass")) == "repair"
        assert route_after_tdd_red(state(False, "reject")) == "repair"
        # Passing RED gate -> routes to build.
        assert route_after_tdd_red(state(True, "pass")) == "build"


def test_model_output_cannot_set_workflow_authority_state() -> None:
    """A model review 'pass' verdict cannot directly transition a requirement to PASS."""
    contract = Contract(
        source=ContractSource(path="requirements.md", sha256="0" * 64),
        requirements=[
            Requirement(
                id="INV-CORE-002",
                statement="Authority boundary must hold.",
                source="requirements.md:L1-L1",
                severity="mandatory",
            )
        ],
    )
    snapshot = ComplianceEngine.initial(contract)
    assert snapshot.entries["INV-CORE-002"].status == RequirementStatus.UNVERIFIED

    # Local evidence carrying a model review 'pass' still only reaches PARTIAL.
    after_local = ComplianceEngine.mark_local_pass(
        snapshot, ["INV-CORE-002"], ["independent-review:pass"]
    )
    assert after_local.entries["INV-CORE-002"].status == RequirementStatus.PARTIAL

    # Only the deterministic remote-verified transition reaches PASS.
    after_remote = ComplianceEngine.mark_remote_verified(
        after_local, ["INV-CORE-002"], ["github-pr:1", "merged-sha:abc"]
    )
    assert after_remote.entries["INV-CORE-002"].status == RequirementStatus.PASS
