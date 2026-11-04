"""REQ-AGENT-006: bounded parallel review-lane concurrency.

Source: CONVERGE_ORCHESTRATOR_2_0_SOURCE_OF_TRUTH.md:L254
[7. Deterministic architecture and agent roles].

These tests pin the deterministic guarantee that the review fanout respects
`max_parallel_reviews` as a hard upper bound on concurrently executing lanes,
even when more lanes are configured, and that ProjectConfig rejects values
outside the documented 1-16 range.
"""

from __future__ import annotations

import json
import pathlib
import threading
import time
import types
import unittest.mock

import pytest
from pydantic import ValidationError

from converge_orchestrator.models import AgentConfig, ProjectConfig, ReviewResult, SpecialtyAnalyzer
from converge_orchestrator.opencode import OpenCodeAdapter

FOUR_REVIEW_ROLES = [
    "correctness_reviewer",
    "architecture_reviewer",
    "security_reviewer",
    "performance_reviewer",
]


def _config(tmp_path: pathlib.Path, max_parallel_reviews: int = 2) -> ProjectConfig:
    repo = tmp_path / "repo"
    repo.mkdir()
    requirements = tmp_path / "architecture.md"
    requirements.write_text("System must bound review concurrency.\n", encoding="utf-8")
    return ProjectConfig(
        repo_path=repo,
        requirements_path=requirements,
        require_spec_read_only=False,
        review_roles=list(FOUR_REVIEW_ROLES),
        max_parallel_reviews=max_parallel_reviews,
        agents={
            "correctness_reviewer": AgentConfig(
                agent="converge-correctness-reviewer",
                model="openai/correctness",
            ),
            "architecture_reviewer": AgentConfig(
                agent="converge-architecture-reviewer",
                model="openai/architecture",
            ),
            "security_reviewer": AgentConfig(
                agent="converge-security-reviewer",
                model="openai/security",
            ),
            "performance_reviewer": AgentConfig(
                agent="converge-performance-reviewer",
                model="openai/performance",
            ),
        },
        specialty_analyzers={
            "performance_reviewer": SpecialtyAnalyzer(
                description="Independent read-only performance reviewer.",
                prompt="You are the performance review lane. Stay read-only.",
            ),
        },
    )


def test_req_agent_006_review_fanout_bounds_concurrency_and_completes_all_lanes(
    tmp_path: pathlib.Path,
) -> None:
    """With 4 configured lanes and max_parallel_reviews=2, the fanout never runs
    more than 2 lanes at once, yet every lane completes and the aggregate contains
    all four reviewer verdicts."""
    cfg = _config(tmp_path, max_parallel_reviews=2)
    adapter = OpenCodeAdapter(cfg)

    payloads = {
        "converge-correctness-reviewer": {
            "verdict": "pass",
            "findings": [],
            "confidence": 0.91,
        },
        "converge-architecture-reviewer": {
            "verdict": "pass",
            "findings": [],
            "confidence": 0.88,
        },
        "converge-security-reviewer": {
            "verdict": "reject",
            "findings": [
                {
                    "severity": "major",
                    "reason": "Policy violation in review lane.",
                    "requirement_id": "SEC-001",
                }
            ],
            "confidence": 0.83,
        },
        "converge-performance-reviewer": {
            "verdict": "pass",
            "findings": [],
            "confidence": 0.85,
        },
    }

    active_lock = threading.Lock()
    active_count = [0]
    max_active_count = [0]
    wave_lock = threading.Lock()
    wave1_count = [0]
    wave2_count = [0]

    # If workers were forced to 1, the first lane would wait forever for a partner
    # and the barrier would time out. With 2 workers we see two waves of 2.
    barrier = threading.Barrier(cfg.max_parallel_reviews, timeout=10)

    def fake_run(command, **kwargs):
        del kwargs
        agent_id = command[command.index("--agent") + 1]
        with active_lock:
            active_count[0] += 1
            if active_count[0] > max_active_count[0]:
                max_active_count[0] = active_count[0]
        try:
            barrier.wait()
            with wave_lock:
                if wave1_count[0] < cfg.max_parallel_reviews:
                    wave1_count[0] += 1
                else:
                    wave2_count[0] += 1
            # Hold the lane open briefly; no network or subprocess call occurs.
            time.sleep(0.05)
        finally:
            with active_lock:
                active_count[0] -= 1
        return types.SimpleNamespace(
            returncode=0,
            stdout=json.dumps(payloads[agent_id]),
        )

    target = "converge_orchestrator.opencode.ExecutionSandbox.run"
    with unittest.mock.patch(target, side_effect=fake_run) as runner:
        result = adapter.invoke("reviewer", "Review this diff", cfg.repo_path)

    assert result.ok
    assert runner.call_count == 4
    assert max_active_count[0] <= cfg.max_parallel_reviews
    # Positive pin: lanes actually executed in two full waves of configured parallelism.
    assert wave1_count[0] == cfg.max_parallel_reviews
    assert wave2_count[0] == cfg.max_parallel_reviews

    aggregate = ReviewResult.model_validate_json(result.output)
    assert aggregate.verdict == "reject"
    assert aggregate.reviewers == {
        "correctness_reviewer": "pass",
        "architecture_reviewer": "pass",
        "security_reviewer": "reject",
        "performance_reviewer": "pass",
    }
    assert len(aggregate.findings) == 1
    assert aggregate.findings[0].reviewer == "security_reviewer"


@pytest.mark.parametrize("bad_value", [0, 17])
def test_req_agent_006_project_config_rejects_max_parallel_reviews_out_of_bounds(
    tmp_path: pathlib.Path,
    bad_value: int,
) -> None:
    """ProjectConfig/model validation rejects max_parallel_reviews below 1 or above 16."""
    repo = tmp_path / "repo"
    repo.mkdir()
    requirements = tmp_path / "architecture.md"
    requirements.write_text("System must bound review concurrency.\n", encoding="utf-8")

    with pytest.raises(ValidationError, match="max_parallel_reviews"):
        ProjectConfig(
            repo_path=repo,
            requirements_path=requirements,
            review_roles=["correctness_reviewer"],
            max_parallel_reviews=bad_value,
            agents={
                "correctness_reviewer": AgentConfig(
                    agent="converge-correctness-reviewer",
                    model="openai/correctness",
                ),
            },
        )
