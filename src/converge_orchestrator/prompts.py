from __future__ import annotations

import json

from .models import Requirement, TaskEnvelope


def contract_excerpt(requirements: list[Requirement], limit: int = 80) -> str:
    return "\n".join(
        f"{requirement.id} | {requirement.source} | {requirement.statement}"
        for requirement in requirements[:limit]
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
    }
    return f"""You are the planning agent in an autonomous software convergence system.
The architecture requirements are immutable. Never propose changing them.
Select ONE smallest high-value change that moves the repository toward compliance.
Inspect the repository before deciding. Avoid broad rewrites and unrelated modernization.
Return ONLY JSON matching this shape: {json.dumps(schema)}
Iteration: {iteration}

REQUIREMENTS:
{contract_excerpt(requirements)}
"""


def builder_prompt(
    task: TaskEnvelope,
    requirements: list[Requirement],
) -> str:
    relevant = task_requirements(task, requirements)
    return f"""Implement the task below in this isolated git worktree.
The architecture requirements are immutable. The orchestrator has supplied the exact target
requirement statements and source anchors below. Do not modify or reinterpret them.
Inspect existing code first. Make the smallest coherent change. Stay inside allowed_paths.
Add or update meaningful tests for changed behavior. Do not push, merge, or modify the base branch.

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
regressions, hidden behavior changes, and violations of the Task Envelope.
Return ONLY JSON matching this shape: {json.dumps(schema)}

TASK:
{task.model_dump_json(indent=2)}
REQUIREMENTS:
{contract_excerpt(requirements)}
DIFF:
{diff_text[-30000:]}
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
tests. Stay inside the original Task Envelope. Do not push or merge.
TARGET REQUIREMENTS:
{contract_excerpt(relevant, limit=len(relevant))}
TASK: {task.model_dump_json(indent=2)}
QUALITY: {quality_json}
REVIEW: {review_json}
"""
