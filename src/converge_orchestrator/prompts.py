from __future__ import annotations

import json

from .models import ComplianceSnapshot, Requirement, TaskEnvelope
from .review_scope import SATISFIED_STATUSES, requirement_statuses


def contract_excerpt(requirements: list[Requirement], limit: int | None = None) -> str:
    selected = requirements if limit is None else requirements[:limit]
    return "\n".join(
        f"{requirement.id} | {requirement.source} | {requirement.statement}"
        for requirement in selected
    )


def task_requirements(
    task: TaskEnvelope,
    requirements: list[Requirement],
) -> list[Requirement]:
    target_ids = set(task.requirement_ids)
    return [requirement for requirement in requirements if requirement.id in target_ids]


def planner_prompt(requirements: list[Requirement], iteration: int) -> str:
    target_id = requirements[0].id if len(requirements) == 1 else "ARCH-017"
    schema = {
        "id": f"{target_id}-0038",
        "requirement_ids": [target_id],
        "title": "Bounded task title",
        "objective": "One measurable objective",
        "constraints": ["Do not change public API"],
        "allowed_paths": ["src/**", "tests/**"],
        "acceptance": ["Relevant test passes"],
        "max_diff_lines": 400,
        "risk": "medium",
        "risk_flags": [],
        "change_kind": "behavior|refactor|docs|config|test_only|other",
        "tdd": {
            "mode": "required|not_applicable",
            "test_paths": ["tests/**"],
            "test_gate": None,
            "expected_failure_pattern": "literal marker unique to the new failing test",
            "rationale": "Why red-before-green is applicable or not applicable",
        },
    }
    if len(requirements) == 1:
        selection_instruction = f"""The deterministic convergence scheduler selected {target_id}.
Plan ONE smallest high-value change for exactly this requirement. `requirement_ids` MUST be
exactly ["{target_id}"]. Do not switch to another requirement even if another change looks easier.
Inspect the repository to find the smallest evidence-backed gap for this target."""
        requirements_label = "DETERMINISTIC TARGET REQUIREMENT"
    else:
        # Compatibility path for callers that do not use the service target scheduler.
        selection_instruction = (
            "Select ONE smallest high-value change that moves the repository toward compliance."
        )
        requirements_label = "REQUIREMENTS"
    return f"""You are the planning agent in an autonomous software convergence system.
The architecture requirements are immutable. Never propose changing them.
{selection_instruction}
Avoid broad rewrites and unrelated modernization.
Classify the task honestly:
- behavior: changes observable runtime behavior, fixes a behavioral bug, or adds behavior;
- refactor: preserves observable behavior;
- docs/config/test_only: only that surface changes;
- other: none of the above.
Behavior-changing tasks MUST use tdd.mode=required. Declare test_paths that may change during
RED and a specific expected_failure_pattern containing a LITERAL output marker that should
appear only after the new failing test is added. This value is not a regular expression and
should identify the new assertion/test specifically. `test_gate` may name an existing
configured/discovered deterministic test gate; null means the orchestrator deterministically
chooses the first available test gate. Never invent or return an arbitrary shell command for
TDD. If no deterministic test gate can exercise the desired behavior, plan a smaller
 test-infrastructure/test_only task first instead of bypassing TDD.
For refactor/docs/config/test_only/other use tdd.mode=not_applicable with a concise rationale.
Return ONLY JSON matching this shape: {json.dumps(schema)}
Iteration: {iteration}

{requirements_label}:
{contract_excerpt(requirements)}
"""


def builder_prompt(
    task: TaskEnvelope,
    requirements: list[Requirement],
    red_evidence: dict | None = None,
) -> str:
    relevant = task_requirements(task, requirements)
    tdd_context = ""
    if task.tdd.mode == "required":
        tdd_context = f"""
TDD RED EVIDENCE:
{json.dumps(red_evidence, ensure_ascii=False, indent=2)}
The orchestrator has already verified a test-only RED phase against the pre-change implementation.
Preserve the exact frozen RED test artifact and now implement the smallest production change needed
to make the same deterministic test gate pass. Do not modify, weaken, delete, skip or xfail the
frozen RED test; GREEN verifies its SHA-256 before accepting the result.
"""
    return f"""Implement the task below in this isolated git worktree.
The architecture requirements are immutable. The orchestrator has supplied the exact target
requirement statements and source anchors below. Do not modify or reinterpret them.
Inspect existing code first. Make the smallest coherent change. Stay inside allowed_paths.
Add or update meaningful tests for changed behavior. Do not push, merge, or modify the base branch.
Preserve existing public entry points. For Node packages, keep existing package.json exports, bin
commands and legacy main/module/type targets; prefer an additive compatibility shim over replacing a
consumer-visible entry point unless the immutable requirement explicitly demands a breaking change.
{tdd_context}
TARGET REQUIREMENTS:
{contract_excerpt(relevant, limit=len(relevant))}

TASK:
{task.model_dump_json(indent=2)}
"""


def tdd_red_prompt(
    task: TaskEnvelope,
    requirements: list[Requirement],
    prior_evidence: dict | None = None,
) -> str:
    relevant = task_requirements(task, requirements)
    evidence = ""
    if prior_evidence is not None:
        serialized = json.dumps(prior_evidence, ensure_ascii=False, indent=2)
        evidence = f"\nPRIOR RED ATTEMPT EVIDENCE:\n{serialized}\n"
    return f"""Prepare ONLY the failing-test (RED) phase for the task below.
Do not implement or modify production behavior. The orchestrator will reject this phase unless every
changed file is inside tdd.test_paths and the declared deterministic test gate produces the expected
new literal failure marker against the old implementation. Do not disable existing tests, weaken
assertions, add skip/xfail markers, or manufacture a failure unrelated to the requested behavior.
Keep the test minimal and evidence-backed. Do not push or merge.
{evidence}
TARGET REQUIREMENTS:
{contract_excerpt(relevant, limit=len(relevant))}

TASK:
{task.model_dump_json(indent=2)}
"""


REVIEW_SCOPE_TARGET_HEADER = (
    "CURRENT TARGET REQUIREMENTS (mandatory acceptance criteria for this candidate):"
)
REVIEW_SCOPE_NON_REGRESSION_HEADER = (
    "ALREADY-SATISFIED REQUIREMENTS - NON-REGRESSION CONTRACT (must not regress):"
)
REVIEW_SCOPE_FUTURE_HEADER = (
    "FUTURE / PENDING REQUIREMENTS - AUTHORITATIVE ROADMAP CONTEXT, "
    "NOT CURRENT BLOCKING ACCEPTANCE CRITERIA:"
)


def review_scope_sections(
    task: TaskEnvelope,
    requirements: list[Requirement],
    compliance: ComplianceSnapshot | None = None,
) -> str:
    """Render the deterministic blocking-scope context for one candidate review."""

    target_ids = set(task.requirement_ids)
    statuses = requirement_statuses(requirements, compliance)
    targets = [requirement for requirement in requirements if requirement.id in target_ids]
    non_regression = [
        requirement
        for requirement in requirements
        if requirement.id not in target_ids
        and statuses[requirement.id] in SATISFIED_STATUSES
    ]
    nonreg_ids = {requirement.id for requirement in non_regression}
    pending = [
        requirement
        for requirement in requirements
        if requirement.id not in target_ids and requirement.id not in nonreg_ids
    ]
    return f"""REVIEW SCOPE (deterministic, derived from the immutable requirements and run state):
BLOCKING SCOPE FOR THIS CANDIDATE = CURRENT TARGET REQUIREMENTS + ALREADY-SATISFIED REQUIREMENTS
(non-regression) + defects actually introduced by this diff + the Task Envelope contract above.
{REVIEW_SCOPE_TARGET_HEADER}
{contract_excerpt(targets, limit=len(targets)) or "(none)"}
{REVIEW_SCOPE_NON_REGRESSION_HEADER}
{contract_excerpt(non_regression, limit=len(non_regression)) or "(none yet)"}
{REVIEW_SCOPE_FUTURE_HEADER}
{contract_excerpt(pending, limit=len(pending)) or "(none)"}
These remain mandatory goals for the overall run, but the current candidate MUST NOT be rejected
merely because it has not implemented them yet. Never issue a blocker whose only basis is a
FUTURE/PENDING requirement, and never convert a future functional requirement into a present
architecture or security invariant. Findings whose only requirement citation is a FUTURE/PENDING
requirement lie outside this candidate's blocking scope and are deterministically discarded.
You must still reject the candidate for an actual security, architecture or compatibility defect
introduced by this diff, or for regressing an ALREADY-SATISFIED requirement; cite the concrete
defect (with file/line evidence) or the in-scope requirement, never the future requirement.
"""


def reviewer_prompt(
    task: TaskEnvelope,
    diff_text: str,
    requirements: list[Requirement],
    compliance: ComplianceSnapshot | None = None,
) -> str:
    schema = {
        "verdict": "pass|reject",
        "findings": [
            {
                "severity": "blocker|major|minor|note",
                "requirement_id": "ARCH-017",
                "file": "src/example.py",
                "line": 12,
                "reason": "Concrete finding",
                "required_fix": "Required correction",
            }
        ],
        "confidence": 0.9,
    }
    return f"""Act as an independent architecture and code reviewer. Do not edit files.
Review against immutable requirements and acceptance criteria. Do not trust the Builder's
narrative as evidence. Reject architectural drift, unnecessary scope, weak tests, security
regressions, hidden behavior changes, violations of the Task Envelope, and dishonest change_kind/TDD
classification. For behavior tasks, reject changes that weaken/remove the RED test instead of making
it pass through the intended implementation. Reject unexplained removals or retargeting of existing
Node package exports, CLI commands, or legacy main/module/type entry points.
Every blocker must be grounded in the BLOCKING SCOPE below.
Return ONLY JSON matching this shape: {json.dumps(schema)}

TASK:
{task.model_dump_json(indent=2)}
{review_scope_sections(task, requirements, compliance)}
DIFF:
{diff_text}
"""


def repair_prompt(
    task: TaskEnvelope,
    requirements: list[Requirement],
    quality: list[dict],
    review: dict | None,
) -> str:
    quality_json = json.dumps(quality, ensure_ascii=False)
    review_json = json.dumps(review, ensure_ascii=False)
    relevant = task_requirements(task, requirements)
    return f"""Repair the current implementation without expanding scope.
Keep requirements unchanged. Fix all required quality-gate or review failures and rerun relevant
tests. Stay inside the original Task Envelope. If the task has verified TDD RED evidence, preserve
the frozen RED test exactly; do not weaken/delete/skip it to obtain GREEN. Do not push or merge.
Review findings whose only requirement citation is a FUTURE/PENDING requirement outside the target
and already-satisfied sets are advisory context only (deterministically downgraded to notes); do not
modify the candidate to satisfy them and never violate the Task Envelope constraints for a finding.
When repairing a Node compatibility finding, retain the old consumer-visible entry point as a shim
and add the new path separately whenever that satisfies the immutable requirement.
TARGET REQUIREMENTS:
{contract_excerpt(relevant, limit=len(relevant))}
TASK: {task.model_dump_json(indent=2)}
QUALITY: {quality_json}
REVIEW: {review_json}
"""
