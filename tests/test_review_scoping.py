"""Deterministic review-scoping regression coverage.

Faithful reproduction of the external-acceptance V9 contradiction: a candidate targeting
ACCEPT-001 (REQ-F92FFC55BA, additive ``simulate_command`` with ``format_output`` still exported)
was rejected by reviewer lanes whose blockers cited only the future requirement ACCEPT-003
(REQ-280A8C4BB0, "format_output must no longer be exported"). Reviewers prescribed removing the
public export, the Builder complied, the deterministic risk classifier correctly raised
``forbidden_public_api_change``, and the repair loop exhausted into a human gate before the first
merged task. These tests pin the corrected blocking-scope semantics:

1. Reviewer context deterministically labels CURRENT TARGET / ALREADY-SATISFIED (non-regression) /
   FUTURE-PENDING (roadmap, non-blocking) requirements; the immutable requirements are never hidden.
2. Blockers whose only requirement citation is a future non-target requirement are demoted to
   notes and cannot drive the aggregate verdict, while blockers on target requirements,
   already-satisfied requirements, unattributed concrete defects, or unknown requirement IDs
   stay blocking (fail closed).
3. When ACCEPT-003 later becomes the current target, demanding the export removal is in scope and
   the ``forbidden_public_api_change`` human-only risk policy remains reachable.
"""

from converge_orchestrator.models import (
    ComplianceEntry,
    ComplianceSnapshot,
    GateResult,
    Requirement,
    RequirementStatus,
    ReviewFinding,
    ReviewResult,
    TaskEnvelope,
    TDDPlan,
)
from converge_orchestrator.policy import DecisionKind, can_integrate
from converge_orchestrator.prompts import repair_prompt, reviewer_prompt
from converge_orchestrator.review_scope import (
    blocking_requirement_ids,
    scope_review,
)

ACCEPT_001 = "REQ-F92FFC55BA"
ACCEPT_002 = "REQ-413A5B74FD"
ACCEPT_003 = "REQ-280A8C4BB0"


def _requirements() -> list[Requirement]:
    return [
        Requirement(
            id=ACCEPT_001,
            statement=(
                "ACCEPT-001: provide simulate_command(command) returning the same result envelope "
                "as run_command without changing the public run_command entry point; the existing "
                "public contract (including format_output) remains exported."
            ),
            source="requirements/acceptance.md#accept-001",
        ),
        Requirement(
            id=ACCEPT_002,
            statement=(
                "ACCEPT-002: deterministic tests prove command text is treated as data and is "
                "never executed."
            ),
            source="requirements/acceptance.md#accept-002",
        ),
        Requirement(
            id=ACCEPT_003,
            statement=(
                "ACCEPT-003: after the migration completes, the legacy public symbol format_output "
                "must no longer be exported."
            ),
            source="requirements/acceptance.md#accept-003",
        ),
    ]


def _task(requirement_id: str, *, constraints: list[str] | None = None) -> TaskEnvelope:
    return TaskEnvelope(
        id=f"{requirement_id}-0001",
        requirement_ids=[requirement_id],
        title="Bounded acceptance task",
        objective="Smallest evidence-backed step for the target requirement.",
        constraints=constraints
        or [
            "Additive only; keep the existing public contract "
            "(run_command and format_output) unchanged."
        ],
        allowed_paths=["src/**", "tests/**"],
        acceptance=["Target requirement evidence passes; existing public exports unchanged."],
        change_kind="behavior",
        tdd=TDDPlan(
            mode="required",
            test_paths=["tests/test_simulate_command.py"],
            expected_failure_pattern="simulate_command is not defined",
        ),
    )


def _compliance(**statuses: str) -> ComplianceSnapshot:
    return ComplianceSnapshot(
        entries={
            requirement_id: ComplianceEntry(
                requirement_id=requirement_id,
                status=RequirementStatus(status),
            )
            for requirement_id, status in statuses.items()
        }
    )


def _pass_gate() -> GateResult:
    return GateResult(name="tests", ok=True, required=True, returncode=0, output="")


def test_reviewer_prompt_labels_future_requirement_as_non_blocking() -> None:
    compliance = _compliance(**{ACCEPT_002: "pass"})
    prompt = reviewer_prompt(_task(ACCEPT_001), "diff", _requirements(), compliance)

    target_header = prompt.index("CURRENT TARGET REQUIREMENTS")
    nonreg_header = prompt.index("ALREADY-SATISFIED REQUIREMENTS")
    future_header = prompt.index("FUTURE / PENDING REQUIREMENTS")
    assert target_header < prompt.index(ACCEPT_001, target_header) < future_header
    assert nonreg_header < prompt.index(ACCEPT_002, nonreg_header) < future_header
    assert future_header < prompt.index(ACCEPT_003, future_header)
    assert "NOT CURRENT BLOCKING ACCEPTANCE CRITERIA" in prompt
    assert "MUST NOT be rejected" in prompt
    assert "deterministically discarded" in prompt
    assert "architecture or security invariant" in prompt


def test_reviewer_prompt_still_supplies_full_immutable_requirements() -> None:
    prompt = reviewer_prompt(_task(ACCEPT_001), "diff", _requirements(), None)
    for requirement in _requirements():
        assert requirement.id in prompt
        assert requirement.statement in prompt


def test_reviewer_prompt_labels_accept_003_as_current_target_when_targeted() -> None:
    prompt = reviewer_prompt(_task(ACCEPT_003), "diff", _requirements(), None)
    target_header = prompt.index("CURRENT TARGET REQUIREMENTS")
    future_header = prompt.index("FUTURE / PENDING REQUIREMENTS")
    assert target_header < prompt.index(ACCEPT_003, target_header) < future_header
    assert prompt.index(ACCEPT_001, future_header) > future_header


def test_repair_prompt_marks_out_of_scope_findings_as_advisory() -> None:
    review = {
        "verdict": "reject",
        "findings": [
            {
                "severity": "note",
                "reason": "format_output is still exported; REQ-280A8C4BB0 requires removal",
                "requirement_id": ACCEPT_003,
            }
        ],
        "scoping": [{"requirement_id": ACCEPT_003, "scoped_severity": "note"}],
    }
    prompt = repair_prompt(_task(ACCEPT_001), _requirements(), [], review)
    assert "advisory context only" in prompt
    assert "never violate the Task Envelope constraints" in prompt


def test_scope_review_demotes_future_citations_but_keeps_real_target_defects() -> None:
    review = ReviewResult(
        verdict="reject",
        findings=[
            ReviewFinding(
                severity="blocker",
                reason=(
                    "format_output is still exported; REQ-280A8C4BB0 explicitly states it must "
                    "no longer be exported"
                ),
                required_fix="Remove format_output from the public export",
                requirement_id=ACCEPT_003,
                reviewer="architecture_reviewer",
            ),
            ReviewFinding(
                severity="blocker",
                reason="Remove or make format_output private per REQ-280A8C4BB0",
                required_fix="Define __all__ without format_output",
                requirement_id=ACCEPT_003,
                reviewer="security_reviewer",
            ),
            ReviewFinding(
                severity="blocker",
                reason=(
                    "__all__ omits format_output, effectively removing the existing public "
                    "export; the task is additive only"
                ),
                required_fix="Restore format_output to the public export surface",
                requirement_id=ACCEPT_001,
                reviewer="correctness_reviewer",
            ),
        ],
        confidence=0.85,
        reviewers={
            "architecture_reviewer": "reject",
            "security_reviewer": "reject",
            "correctness_reviewer": "reject",
        },
    )
    scoped, scoping = scope_review(
        review=review,
        target_requirement_ids={ACCEPT_001},
        requirements=_requirements(),
        compliance=ComplianceSnapshot(),
    )

    assert scoped.verdict == "reject"
    assert [(finding.severity, finding.reviewer) for finding in scoped.findings] == [
        ("note", "architecture_reviewer"),
        ("note", "security_reviewer"),
        ("blocker", "correctness_reviewer"),
    ]
    assert [item["requirement_id"] for item in scoping["demoted_findings"]] == [
        ACCEPT_003,
        ACCEPT_003,
    ]
    assert scoping["reviewers_after"] == {
        "architecture_reviewer": "pass",
        "security_reviewer": "pass",
        "correctness_reviewer": "reject",
    }
    assert scoping["reviewers_before"] == {
        "architecture_reviewer": "reject",
        "security_reviewer": "reject",
        "correctness_reviewer": "reject",
    }
    assert scoped.reviewers == scoping["reviewers_after"]


def test_scope_review_passes_candidate_when_only_future_cited_blockers_existed() -> None:
    review = ReviewResult(
        verdict="reject",
        findings=[
            ReviewFinding(
                severity="blocker",
                reason="format_output is still exported; REQ-280A8C4BB0 requires removal",
                requirement_id=ACCEPT_003,
                reviewer="architecture_reviewer",
            ),
            ReviewFinding(
                severity="blocker",
                reason="Remove format_output from the module's public export per REQ-280A8C4BB0",
                requirement_id=ACCEPT_003,
                reviewer="security_reviewer",
            ),
        ],
        confidence=0.8,
        reviewers={
            "architecture_reviewer": "reject",
            "security_reviewer": "reject",
            "correctness_reviewer": "pass",
        },
    )
    scoped, scoping = scope_review(
        review=review,
        target_requirement_ids={ACCEPT_001},
        requirements=_requirements(),
        compliance=ComplianceSnapshot(),
    )

    assert scoped.verdict == "pass"
    assert all(finding.severity == "note" for finding in scoped.findings)
    assert len(scoping["demoted_findings"]) == 2
    assert scoped.reviewers["correctness_reviewer"] == "pass"


def test_scope_review_keeps_non_regression_blockers_blocking() -> None:
    compliance = _compliance(**{ACCEPT_002: "pass"})
    review = ReviewResult(
        verdict="reject",
        findings=[
            ReviewFinding(
                severity="blocker",
                reason=(
                    "Candidate rewrote the verified ACCEPT-002 data-only command handling; "
                    "command text would execute again"
                ),
                requirement_id=ACCEPT_002,
                reviewer="architecture_reviewer",
            )
        ],
        reviewers={"architecture_reviewer": "reject"},
    )
    scoped, scoping = scope_review(
        review=review,
        target_requirement_ids={ACCEPT_001},
        requirements=_requirements(),
        compliance=compliance,
    )

    assert scoped.verdict == "reject"
    assert scoped.findings[0].severity == "blocker"
    assert scoping == {}


def test_scope_review_keeps_unattributed_defect_blockers_blocking() -> None:
    review = ReviewResult(
        verdict="reject",
        findings=[
            ReviewFinding(
                severity="blocker",
                reason="Candidate diff introduces a shell injection in the new helper",
                reviewer="security_reviewer",
            )
        ],
        reviewers={"security_reviewer": "reject"},
    )
    scoped, scoping = scope_review(
        review=review,
        target_requirement_ids={ACCEPT_001},
        requirements=_requirements(),
        compliance=ComplianceSnapshot(),
    )

    assert scoped.verdict == "reject"
    assert scoped.findings[0].severity == "blocker"
    assert scoping == {}


def test_scope_review_keeps_major_only_rejection_blocking() -> None:
    review = ReviewResult(
        verdict="reject",
        findings=[
            ReviewFinding(
                severity="major",
                reason="Test coverage is weaker than the existing suite",
                reviewer="correctness_reviewer",
            )
        ],
        reviewers={"correctness_reviewer": "reject"},
    )
    scoped, scoping = scope_review(
        review=review,
        target_requirement_ids={ACCEPT_001},
        requirements=_requirements(),
        compliance=ComplianceSnapshot(),
    )

    assert scoped.verdict == "reject"
    assert scoping == {}


def test_scope_review_fails_closed_for_unknown_requirement_ids() -> None:
    review = ReviewResult(
        verdict="reject",
        findings=[
            ReviewFinding(
                severity="blocker",
                reason="Unknown citation",
                requirement_id="REQ-DOESNOTEXIST",
                reviewer="architecture_reviewer",
            )
        ],
        reviewers={"architecture_reviewer": "reject"},
    )
    scoped, scoping = scope_review(
        review=review,
        target_requirement_ids={ACCEPT_001},
        requirements=_requirements(),
        compliance=ComplianceSnapshot(),
    )

    assert scoped.verdict == "reject"
    assert scoped.findings[0].severity == "blocker"
    assert scoping == {}


def test_scope_review_single_reviewer_shape_without_lane_map() -> None:
    review = ReviewResult(
        verdict="reject",
        findings=[
            ReviewFinding(
                severity="blocker",
                reason="format_output must no longer be exported per REQ-280A8C4BB0",
                requirement_id=ACCEPT_003,
            )
        ],
    )
    scoped, scoping = scope_review(
        review=review,
        target_requirement_ids={ACCEPT_001},
        requirements=_requirements(),
        compliance=ComplianceSnapshot(),
    )

    assert scoped.verdict == "pass"
    assert len(scoping["demoted_findings"]) == 1

    major_only = ReviewResult(
        verdict="reject",
        findings=[ReviewFinding(severity="major", reason="weak tests")],
    )
    scoped_major, scoping_major = scope_review(
        review=major_only,
        target_requirement_ids={ACCEPT_001},
        requirements=_requirements(),
        compliance=ComplianceSnapshot(),
    )
    assert scoped_major.verdict == "reject"
    assert scoping_major == {}


def test_accept_003_removal_is_in_scope_when_it_is_the_current_target() -> None:
    review = ReviewResult(
        verdict="reject",
        findings=[
            ReviewFinding(
                severity="blocker",
                reason="format_output must no longer be exported per ACCEPT-003",
                required_fix="Remove format_output from the public export",
                requirement_id=ACCEPT_003,
                reviewer="security_reviewer",
            )
        ],
        reviewers={"security_reviewer": "reject"},
    )
    scoped, scoping = scope_review(
        review=review,
        target_requirement_ids={ACCEPT_003},
        requirements=_requirements(),
        compliance=ComplianceSnapshot(),
    )

    assert scoped.verdict == "reject"
    assert scoped.findings[0].severity == "blocker"
    assert scoping == {}
    assert blocking_requirement_ids(
        target_requirement_ids={ACCEPT_003},
        requirements=_requirements(),
        compliance=ComplianceSnapshot(),
    ) == {ACCEPT_003}
    assert ACCEPT_002 not in blocking_requirement_ids(
        target_requirement_ids={ACCEPT_003},
        requirements=_requirements(),
        compliance=ComplianceSnapshot(),
    )


def test_blocking_requirement_ids_cover_targets_and_satisfied_requirements() -> None:
    compliance = _compliance(**{ACCEPT_002: "pass", ACCEPT_003: "partial"})
    assert blocking_requirement_ids(
        target_requirement_ids={ACCEPT_001},
        requirements=_requirements(),
        compliance=compliance,
    ) == {ACCEPT_001, ACCEPT_002, ACCEPT_003}


def test_policy_still_interrupts_for_public_api_change_for_operator_approval() -> None:
    decision = can_integrate(
        expected_spec_hash="abc",
        current_spec_hash="abc",
        gates=[_pass_gate()],
        review=ReviewResult(verdict="pass"),
        compliance=ComplianceSnapshot(),
        risk_flags=["forbidden_public_api_change"],
    )
    assert decision.kind == DecisionKind.INTERRUPT
    assert decision.reason == "HUMAN_RISK_POLICY"