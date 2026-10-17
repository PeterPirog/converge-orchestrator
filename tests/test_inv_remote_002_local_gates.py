"""Lock INV-REMOTE-002: absent branch protection never weakens local acceptance gates.

INV-REMOTE-002 (CONVERGE_ORCHESTRATOR_2_0_SOURCE_OF_TRUTH.md:L162 [4. Global invariants]):
Absence of remote branch protection SHALL NOT remove or weaken Converge local acceptance gates.

INV-REMOTE-001 established that branch protection is an *additional external enforcement layer*,
not the source of local acceptance semantics. INV-REMOTE-002 pins the other side of that boundary:
when the remote branch is *unprotected*, Converge's local acceptance gates must still be fully
enforced and unweakened. These deterministic offline tests lock that against future drift:

* local CI observation — an authoritative ``branch_unprotected`` policy with zero required checks
  still evaluates every observed check, so a failing observed check is a hard ``fail`` (the local
  CI gate is neither removed nor relaxed);
* positive control — the same gate is not uniformly blocking: a pending check stays ``pending``
  and a genuinely green observation is accepted as ``pass``;
* integration policy — ``can_integrate`` blocks on a failing required quality gate
  (``QUALITY_GATE_FAILED``) and on a rejecting review (``REVIEW_REJECTED``) with no remote
  branch-protection input at all, so an unprotected branch cannot buy integration;
* compliance promotion — ``ComplianceEngine.mark_local_pass`` caps a local-only verdict at
  ``PARTIAL`` and can never promote an entry to ``PASS`` (that is the remote-verified path), so an
  absent remote gate cannot be substituted by a local promotion;
* confinement — the local gate modules (``quality.py``, ``policy.py``, ``compliance.py``) carry no
  remote branch-protection relaxation vocabulary (``branch_unprotected``, ``required_checks``), so
  they cannot condition local gates on protection state.

No network, model, subprocess, or git calls: the GitHub transport is stubbed in memory and the
carrier scan reads module sources from disk.
"""

from __future__ import annotations

from pathlib import Path

import converge_orchestrator
from converge_orchestrator.compliance import ComplianceEngine
from converge_orchestrator.github import GitHubAdapter, RemotePolicy
from converge_orchestrator.models import (
    ComplianceEntry,
    ComplianceSnapshot,
    GateResult,
    ProjectConfig,
    RequirementStatus,
    ReviewResult,
)
from converge_orchestrator.policy import DecisionKind, can_integrate

# Remote branch-protection relaxation vocabulary that must stay out of the local gate modules.
_RELAXATION_TOKENS = ("branch_unprotected", "required_checks")

# The local acceptance-gate modules whose decisions must never depend on remote protection state.
_LOCAL_GATE_MODULES = ("quality.py", "policy.py", "compliance.py")


def _package_sources() -> dict[str, str]:
    package_dir = Path(converge_orchestrator.__file__).resolve().parent
    return {
        path.name: path.read_text(encoding="utf-8") for path in sorted(package_dir.glob("*.py"))
    }


def _config(tmp_path: Path) -> ProjectConfig:
    return ProjectConfig(
        repo_path=tmp_path,
        requirements_path=tmp_path / "architecture.md",
        github_repo="owner/repo",
        base_branch="main",
        agents={},
    )


class _StubGitHubAdapter(GitHubAdapter):
    def __init__(self, config: ProjectConfig, responses: dict[str, dict]):
        super().__init__(config)
        self.responses = responses

    def _api_json(self, endpoint: str, **kwargs):  # type: ignore[no-untyped-def]
        return self.responses[endpoint]


def _ci_adapter(tmp_path: Path, *, checks: list[dict]) -> _StubGitHubAdapter:
    return _StubGitHubAdapter(
        _config(tmp_path),
        {
            "repos/owner/repo/commits/abc/check-runs": {"check_runs": checks},
            "repos/owner/repo/commits/abc/status": {"statuses": []},
        },
    )


def _unprotected_policy() -> RemotePolicy:
    return RemotePolicy(
        base_branch="main",
        protected=False,
        authoritative=True,
        source="branch_unprotected",
    )


def _no_regression_snapshot() -> ComplianceSnapshot:
    return ComplianceSnapshot(entries={}, mandatory_regressions=0)


def test_unprotected_policy_failing_observed_check_is_a_fail(tmp_path: Path) -> None:
    """Absent branch protection must not remove or weaken the local CI gate."""
    adapter = _ci_adapter(
        tmp_path,
        checks=[{"name": "CI", "status": "completed", "conclusion": "failure"}],
    )

    result = adapter.ci_status("abc", _unprotected_policy())

    assert result.status == "fail"
    recorded = next(item for item in result.checks if item.get("kind") == "remote_policy")
    assert recorded["protected"] is False
    assert recorded["authoritative"] is True
    assert recorded["source"] == "branch_unprotected"
    assert recorded["required_checks"] == []


def test_unprotected_policy_is_not_uniformly_blocking(tmp_path: Path) -> None:
    """Positive control: the local CI gate discriminates pending vs. passing, not a flat block."""
    pending = _ci_adapter(
        tmp_path,
        checks=[{"name": "CI", "status": "in_progress", "conclusion": None}],
    )
    passing = _ci_adapter(
        tmp_path,
        checks=[{"name": "CI", "status": "completed", "conclusion": "success"}],
    )

    policy = _unprotected_policy()
    assert pending.ci_status("abc", policy).status == "pending"
    assert passing.ci_status("abc", policy).status == "pass"


def test_can_integrate_blocks_on_failing_required_quality_gate(tmp_path: Path) -> None:
    """A failing required quality gate blocks even when the local CI observation is green."""
    adapter = _ci_adapter(
        tmp_path,
        checks=[{"name": "CI", "status": "completed", "conclusion": "success"}],
    )
    # The local CI observation is permissive under an unprotected branch...
    assert adapter.ci_status("abc", _unprotected_policy()).status == "pass"
    # ...but a failing required quality gate still blocks integration.
    decision = can_integrate(
        expected_spec_hash="spec",
        current_spec_hash="spec",
        gates=[GateResult(name="tests", ok=False, required=True, returncode=1, output="failed")],
        review=ReviewResult(verdict="pass"),
        compliance=_no_regression_snapshot(),
        risk_flags=[],
    )
    assert decision.kind == DecisionKind.BLOCK
    assert decision.reason == "QUALITY_GATE_FAILED"


def test_can_integrate_blocks_on_rejecting_review(tmp_path: Path) -> None:
    """A rejecting review blocks even when the local quality gate is green."""
    adapter = _ci_adapter(
        tmp_path,
        checks=[{"name": "CI", "status": "completed", "conclusion": "success"}],
    )
    assert adapter.ci_status("abc", _unprotected_policy()).status == "pass"

    decision = can_integrate(
        expected_spec_hash="spec",
        current_spec_hash="spec",
        gates=[GateResult(name="tests", ok=True, required=True, returncode=0, output="ok")],
        review=ReviewResult(verdict="reject"),
        compliance=_no_regression_snapshot(),
        risk_flags=[],
    )
    assert decision.kind == DecisionKind.BLOCK
    assert decision.reason == "REVIEW_REJECTED"


def test_mark_local_pass_never_promotes_to_pass() -> None:
    """A local-only compliance verdict can never substitute for a remote-verified PASS."""
    starting_states = (
        RequirementStatus.UNVERIFIED,
        RequirementStatus.FAIL,
        RequirementStatus.PARTIAL,
        RequirementStatus.BLOCKED,
    )
    snapshot = ComplianceSnapshot(
        entries={
            f"REQ-{index}": ComplianceEntry(requirement_id=f"REQ-{index}", status=status)
            for index, status in enumerate(starting_states)
        }
    )

    promoted = ComplianceEngine.mark_local_pass(snapshot, list(snapshot.entries), ["local-only"])

    for entry in promoted.entries.values():
        assert entry.status != RequirementStatus.PASS
        assert entry.status == RequirementStatus.PARTIAL
        assert "local-only" in entry.evidence


def test_local_gate_modules_expose_no_branch_protection_relaxation() -> None:
    """Local gates carry no branch-protection vocabulary that could condition them on protection."""
    sources = _package_sources()
    # The vocabulary is a real boundary: it must exist in the external surface so the confinement
    # below is a genuine boundary, not a vacuous absence across the whole package.
    assert any(token in sources["github.py"] for token in _RELAXATION_TOKENS), (
        "branch-protection relaxation vocabulary vanished from the package, so the confinement "
        f"test is no longer meaningful: {_RELAXATION_TOKENS}"
    )
    offenders = {
        name: [token for token in _RELAXATION_TOKENS if token in sources[name]]
        for name in _LOCAL_GATE_MODULES
    }
    offenders = {name: hits for name, hits in offenders.items() if hits}
    assert not offenders, (
        "local acceptance gates reference remote branch-protection vocabulary, which could "
        f"condition local gates on protection state: {offenders}"
    )
