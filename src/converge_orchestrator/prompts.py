from __future__ import annotations

import json
from pathlib import Path

from .models import Requirement, TaskEnvelope


def contract_excerpt(requirements: list[Requirement], limit: int = 80) -> str:
    return "\n".join(f"{r.id} | {r.source} | {r.statement}" for r in requirements[:limit])


def planner_prompt(requirements: list[Requirement], iteration: int) -> str:
    return f"""You are the planning agent in an autonomous software convergence system.
The architecture requirements are immutable. Never propose changing them.
Select ONE smallest high-value change that moves the repository toward compliance.
Inspect the repository before deciding. Avoid broad rewrites.
Return ONLY JSON with keys: id, requirement_ids, title, objective, acceptance, risk.
Iteration: {iteration}

REQUIREMENTS:\n{contract_excerpt(requirements)}
"""


def builder_prompt(task: TaskEnvelope, requirements_path: Path) -> str:
    return f"""Implement the task below in this isolated git worktree.
The architecture specification at {requirements_path} is READ ONLY and authoritative.
Do not modify it. Inspect existing code first. Make the smallest coherent change.
Add or update meaningful tests. Do not push, merge, or modify the base branch.

TASK:\n{task.model_dump_json(indent=2)}
"""


def reviewer_prompt(task: TaskEnvelope, diff_text: str, requirements: list[Requirement]) -> str:
    return f"""Act as an independent architecture and code reviewer. Do not edit files.
Review against immutable requirements and acceptance criteria. Reject drift, unnecessary scope,
weak tests, security regressions, and hidden behavioral changes.
Return ONLY JSON: {{\"verdict\":\"approve|reject\",\"findings\":[{{\"severity\":\"major|minor\",\"reason\":\"...\",\"required_fix\":\"...\"}}]}}.
TASK:\n{task.model_dump_json(indent=2)}\nREQUIREMENTS:\n{contract_excerpt(requirements)}\nDIFF:\n{diff_text[-30000:]}
"""


def repair_prompt(task: TaskEnvelope, quality: list[dict], review: dict | None) -> str:
    return f"""Repair the current implementation without expanding scope. Keep requirements unchanged.
Fix all required quality-gate or review failures, then rerun relevant tests. Do not push or merge.
TASK: {task.model_dump_json(indent=2)}\nQUALITY: {json.dumps(quality, ensure_ascii=False)}\nREVIEW: {json.dumps(review, ensure_ascii=False)}
"""
