from __future__ import annotations

import json

from .models import Requirement, TaskEnvelope


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
    schema = {
        "id": "ARCH-017-0038",
        "requirement_ids": ["ARCH-017"],
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
            "expected_failure_pattern": "specific new-test failure regex",
            "rationale": "Why red-before-green is applicable or not applicable",
        },
    }
    return f"""You are the planning agent in an autonomous software convergence system.
The architecture requirements are immutable. Never propose changing them.
Select ONE smallest high-value change that moves the repository toward compliance.
Inspect the repository before deciding. Avoid broad rewrites and unrelated modernization.
Classify the task honestly:
- behavior: changes observable runtime behavior, fixes a behavioral bug, or adds behavior;
- refactor: preserves observable behavior;
- docs/config/test_only: only that surface changes;
- other: none of the above.
Behavior-changing tasks MUST use tdd.mode=required. Declare test_paths that may change during the RED
phase and a specific expected_failure_pattern that should appear only after the new failing test is
added. `test_gate` may name an existing configured/discovered deterministic test gate; null means the
orchestrator deterministically chooses the first available test gate. Never invent or return an
arbitrary shell command for TDD. If no deterministic test gate can exercise the desired behavior,
plan a smaller test-infrastructure/test_only task first instead of bypassing TDD.
For refactor/docs/config/test_only/other use tdd.mode=not_applicable with a concise rationale.
Return ONLY JSON matching this shape: {json.dumps(schema)}
Iteration: {iteration}

REQUIREMENTS:
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
Preserve the intent of that test and now implement the smallest production change needed to make the
same deterministic test gate pass. Do not weaken/delete the test merely to obtain GREEN.
"""
    return f"""Implement the task below in this isolated git worktree.
The architecture requirements are immutable. The orchestrator has supplied the exact target
requirement statements and source anchors below. Do not modify or reinterpret them.
Inspect existing code first. Make the smallest coherent change. Stay inside allowed_paths.
Add or update meaningful tests for changed behavior. Do not push, merge, or modify the base branch.
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
    evidence = (
        ""
        if prior_evidence is None
        else f"\nPRIOR RED ATTEMPT EVIDENCE:\n{json.dumps(prior_evidence, ensure_ascii=False, indent=2)}\n"
    )
    return f"""Prepare ONLY the failing-test (RED) phase for the task below.
Do not implement or modify production behavior. The orchestrator will reject this phase unless every
changed file is inside tdd.test_paths and the declared deterministic test gate produces the expected
new failure against the old implementation. Do not disable existing tests, weaken assertions, add
skip/xfail markers, or manufacture a failure unrelated to the requested behavior. Keep the test
minimal and evidence-backed. Do not push or merge.
{evidence}
TARGET REQUIREMENTS:
{contract_excerpt(relevant, limit=len(relevant))}

TASK:
{task.model_dump_json(indent=2)}
"""


def reviewer_prompt(
    task: TaskEnvelope,
    diff_text: str,
    requirements: list[Requirement],
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
it pass through the intended implementation.
Return ONLY JSON matching this shape: {json.dumps(schema)}

TASK:
{task.model_dump_json(indent=2)}
REQUIREMENTS:
{contract_excerpt(requirements)}
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
the intent of that test; do not weaken/delete/skip it to obtain GREEN. Do not push or merge.
TARGET REQUIREMENTS:
{contract_excerpt(relevant, limit=len(relevant))}
TASK: {task.model_dump_json(indent=2)}
QUALITY: {quality_json}
REVIEW: {review_json}
"""
