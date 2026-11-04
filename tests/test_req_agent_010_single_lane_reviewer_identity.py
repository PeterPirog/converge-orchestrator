"""Pin REQ-AGENT-010: model-backed reviewer output SHALL pass deterministic identity validation.

REQ-AGENT-010 (CONVERGE_ORCHESTRATOR_2_0_SOURCE_OF_TRUTH.md:L258
[7. Deterministic architecture and agent roles]): Model-backed role output that affects workflow
state SHALL pass deterministic schema and identity validation before it can change routing,
task state, review state, risk state, compliance, or integration eligibility.

When no review_roles fan-out is configured, a single-lane reviewer invocation must stamp the
parsed ``ReviewResult`` with the actually-invoked lane identity
(``reviewers={'reviewer': verdict}``; unattributed findings attributed to ``'reviewer'``)
before that output can change review state, routing, or integration eligibility.
"""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from converge_orchestrator.models import AgentConfig, ProjectConfig, ReviewResult
from converge_orchestrator.opencode import OpenCodeAdapter

_TARGET_ID = "REQ-AGENT-010"


def _single_lane_config(tmp_path: Path) -> ProjectConfig:
    """Return a minimal controller-owned ProjectConfig with no review fan-out."""
    repo = tmp_path / "repo"
    repo.mkdir()
    requirements = tmp_path / "architecture.md"
    requirements.write_text(
        f"System must satisfy {_TARGET_ID} deterministic reviewer identity validation.\n",
        encoding="utf-8",
    )
    state_dir = tmp_path / "state"
    state_dir.mkdir()
    return ProjectConfig(
        repo_path=repo,
        requirements_path=requirements,
        state_dir=state_dir,
        worktree_dir=state_dir / "worktrees",
        review_roles=[],
        max_parallel_reviews=1,
        agents={
            "reviewer": AgentConfig(agent="converge-reviewer", model="openai/reviewer"),
        },
        require_spec_read_only=False,
    )


def test_single_lane_reviewer_replaces_fabricated_reviewers_identity(
    tmp_path: Path,
) -> None:
    """A fabricated reviewers map from the model is replaced by the invoked lane identity."""
    cfg = _single_lane_config(tmp_path)
    adapter = OpenCodeAdapter(cfg)

    fabricated = json.dumps(
        {
            "verdict": "pass",
            "findings": [
                {
                    "severity": "note",
                    "reason": "Model attempts to claim a fabricated reviewer identity.",
                    "required_fix": "Ignore the fabricated identity.",
                }
            ],
            "reviewers": {"fabricated_reviewer": "pass"},
        }
    )

    def fake_run(command, **kwargs):
        del command, kwargs
        return SimpleNamespace(returncode=0, stdout=fabricated)

    target = "converge_orchestrator.opencode.ExecutionSandbox.run"
    with patch(target, side_effect=fake_run) as runner:
        result = adapter.invoke("reviewer", "Review this diff", cfg.repo_path)

    assert result.ok is True
    assert runner.call_count == 1

    review = ReviewResult.model_validate_json(result.output)
    assert review.verdict == "pass"
    assert review.reviewers == {"reviewer": "pass"}, (
        "REQ-AGENT-010 REVIEWERS IDENTITY VIOLATION: single-lane reviewer output retained a "
        "fabricated reviewers map instead of stamping the invoked lane identity"
    )
    assert review.findings[0].reviewer == "reviewer", (
        "REQ-AGENT-010 REVIEWERS IDENTITY VIOLATION: unattributed single-lane finding was not "
        "stamped with the invoked lane identity"
    )
