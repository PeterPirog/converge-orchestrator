"""Lock INV-INTEGRITY-001: candidate integration authorization fails closed on any non-PASS gate.

INV-INTEGRITY-001 (CONVERGE_ORCHESTRATOR_2_0_SOURCE_OF_TRUTH.md:L150 [4. Global invariants]):
Converge SHALL NOT authorize or record a candidate as an accepted integration while any
applicable mandatory deterministic gate, configured review lane, risk constraint, Source-of-Truth
integrity check, scope constraint, or remote integration gate is non-PASS.

These deterministic tests pin the integration-authorization gate (``can_integrate``) and the
surfaces it delegates to: ``required_gates_pass`` (mandatory deterministic gates, the required
``diff_scope`` scope constraint, and remote/CI-typed required gates) and ``paths_within_allowlist``
(scope allowlist). A single all-green positive control proves the gate is fail-closed, not
uniformly blocking.

No network, models, subprocesses, or git are invoked: every surface exercised here is pure.
"""

from __future__ import annotations

import pytest

from converge_orchestrator.git import paths_within_allowlist
from converge_orchestrator.models import ComplianceSnapshot, GateResult, ReviewResult
from converge_orchestrator.policy import Decision, DecisionKind, can_integrate
from converge_orchestrator.quality import required_gates_pass

_SPEC_HASH = "spec-hash-abc123"
_MUTATED_SPEC_HASH = "spec-hash-rotated"


def _gate(name: str, ok: bool, required: bool = True) -> GateResult:
    """Build a deterministic gate result with the given pass/fail and required/optional state."""
    return GateResult(name=name, ok=ok, required=required, returncode=(0 if ok else 1), output="")


def _decision(overrides: dict[str, object]) -> Decision:
    """Authorize a candidate over an all-green baseline, applying single-axis overrides.

    The baseline mirrors a fully green integration: a matching Source-of-Truth spec hash, a
    passing required deterministic gate, a pass review verdict, zero mandatory regressions, and
    no risk flags. Each non-PASS class under test flips exactly one axis; nothing else changes,
    so the emitted reason isolates that single axis.
    """
    baseline: dict[str, object] = {
        "expected_spec_hash": _SPEC_HASH,
        "current_spec_hash": _SPEC_HASH,
        "gates": [_gate("tests", ok=True)],
        "review": ReviewResult(verdict="pass"),
        "compliance": ComplianceSnapshot(),
        "risk_flags": [],
    }
    baseline.update(overrides)
    return can_integrate(**baseline)  # type: ignore[arg-type]


@pytest.mark.parametrize(
    ("expected_reason", "overrides"),
    [
        pytest.param(
            "SPEC_CHANGED",
            {"current_spec_hash": _MUTATED_SPEC_HASH},
            id="spec_changed",
        ),
        pytest.param(
            "QUALITY_GATE_FAILED",
            {"gates": [_gate("tests", ok=False)]},
            id="quality_gate_failed",
        ),
        pytest.param(
            "REVIEW_REJECTED",
            {"review": ReviewResult(verdict="reject")},
            id="review_rejected",
        ),
        pytest.param(
            "RISK_POLICY_BLOCKED",
            {"risk_flags": ["secret_material_detected"]},
            id="risk_policy_blocked",
        ),
        pytest.param(
            "HUMAN_RISK_POLICY",
            {"risk_flags": ["contradictory_requirements"]},
            id="human_risk_policy",
        ),
        pytest.param(
            "ARCHITECTURE_REGRESSION",
            {"compliance": ComplianceSnapshot(mandatory_regressions=1)},
            id="architecture_regression",
        ),
    ],
)
def test_non_pass_class_never_authorizes(
    expected_reason: str, overrides: dict[str, object]
) -> None:
    """Every single non-PASS class yields a non-ALLOW Decision (INV-INTEGRITY-001)."""
    decision = _decision(overrides)
    assert decision.kind is not DecisionKind.ALLOW
    assert decision.reason == expected_reason


def test_all_green_candidate_is_authorized() -> None:
    """Positive control: the gate authorizes only when every axis is PASS."""
    decision = _decision({})
    assert decision.kind is DecisionKind.ALLOW
    assert decision.reason == "ALL_GATES_PASS"


def test_failing_required_diff_scope_gate_blocks_integration() -> None:
    """A failing required diff_scope scope-constraint gate blocks authorization."""
    decision = _decision({"gates": [_gate("diff_scope", ok=False)]})
    assert decision.kind is not DecisionKind.ALLOW
    assert decision.reason == "QUALITY_GATE_FAILED"


def test_failing_required_remote_ci_gate_blocks_integration() -> None:
    """A failing remote/CI-typed required gate blocks authorization."""
    decision = _decision({"gates": [_gate("remote_ci", ok=False)]})
    assert decision.kind is not DecisionKind.ALLOW
    assert decision.reason == "QUALITY_GATE_FAILED"


def test_failing_non_required_gate_does_not_block_integration() -> None:
    """A failing non-required (optional) gate does not prevent authorization."""
    decision = _decision(
        {"gates": [_gate("tests", ok=True), _gate("lint", ok=False, required=False)]}
    )
    assert decision.kind is DecisionKind.ALLOW
    assert decision.reason == "ALL_GATES_PASS"


@pytest.mark.parametrize(
    ("gates", "expected"),
    [
        pytest.param([], True, id="no_gates"),
        pytest.param([_gate("tests", ok=True)], True, id="all_required_pass"),
        pytest.param([_gate("diff_scope", ok=False)], False, id="required_diff_scope_fail"),
        pytest.param(
            [_gate("tests", ok=True), _gate("remote_ci", ok=False)],
            False,
            id="required_ci_fail",
        ),
        pytest.param(
            [_gate("tests", ok=True), _gate("lint", ok=False, required=False)],
            True,
            id="optional_fail_ignored",
        ),
    ],
)
def test_required_gates_pass_only_honors_required_gates(
    gates: list[GateResult], expected: bool
) -> None:
    """``required_gates_pass`` gates on required gates only, ignoring optional ones."""
    assert required_gates_pass(gates) is expected


def test_paths_within_allowlist_rejects_out_of_scope_candidate_path() -> None:
    """An out-of-scope candidate path is rejected by the allowlist."""
    assert paths_within_allowlist(["src/converge_orchestrator/feature.py"], ["tests/**"]) is False


def test_paths_within_allowlist_accepts_in_scope_candidate_paths() -> None:
    """In-scope candidate paths are accepted by the allowlist."""
    assert paths_within_allowlist(["tests/test_feature.py"], ["tests/**"]) is True
    assert paths_within_allowlist(
        ["tests/test_a.py", "docs/guide.md"], ["tests/**", "docs/**"]
    ) is True


def test_paths_within_allowlist_empty_patterns_allow_all() -> None:
    """An empty allowlist imposes no scope restriction."""
    assert paths_within_allowlist(["anywhere/feature.py"], []) is True
