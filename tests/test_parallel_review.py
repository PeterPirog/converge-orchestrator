from __future__ import annotations

import json
from pathlib import Path
from threading import Barrier
from types import SimpleNamespace
from unittest.mock import patch

from converge_orchestrator.models import ProjectConfig, ReviewResult
from converge_orchestrator.opencode import OpenCodeAdapter
from converge_orchestrator.opencode_config import build_opencode_config


REVIEW_ROLES = [
    "correctness_reviewer",
    "architecture_reviewer",
    "security_reviewer",
]


def _config(tmp_path: Path) -> ProjectConfig:
    repo = tmp_path / "repo"
    repo.mkdir()
    requirements = tmp_path / "architecture.md"
    requirements.write_text("System must remain secure.\n", encoding="utf-8")
    return ProjectConfig(
        repo_path=repo,
        requirements_path=requirements,
        require_spec_read_only=False,
        review_roles=REVIEW_ROLES,
        max_parallel_reviews=3,
        agents={
            "correctness_reviewer": {
                "agent": "converge-correctness-reviewer",
                "model": "openai/correctness",
            },
            "architecture_reviewer": {
                "agent": "converge-architecture-reviewer",
                "model": "openai/architecture",
            },
            "security_reviewer": {
                "agent": "converge-security-reviewer",
                "model": "openai/security",
            },
        },
    )


def test_parallel_review_uses_read_only_specialized_agents(tmp_path: Path) -> None:
    payload = build_opencode_config(_config(tmp_path))

    for agent_id in (
        "converge-correctness-reviewer",
        "converge-architecture-reviewer",
        "converge-security-reviewer",
    ):
        agent = payload["agent"][agent_id]
        assert agent["permission"]["edit"] == "deny"
        assert agent["permission"]["bash"]["*"] == "deny"
        assert agent["permission"]["task"] == "deny"
        assert agent["permission"]["external_directory"] == "deny"


def test_parallel_review_runs_concurrently_and_one_reject_blocks(tmp_path: Path) -> None:
    cfg = _config(tmp_path)
    adapter = OpenCodeAdapter(cfg)
    barrier = Barrier(3)
    payloads = {
        "converge-correctness-reviewer": {
            "verdict": "pass",
            "findings": [],
            "confidence": 0.91,
        },
        "converge-architecture-reviewer": {
            "verdict": "reject",
            "findings": [
                {
                    "severity": "major",
                    "reason": "Dependency direction violates the target boundary.",
                    "required_fix": "Restore the required dependency direction.",
                    "requirement_id": "ARCH-001",
                }
            ],
            "confidence": 0.88,
        },
        "converge-security-reviewer": {
            "verdict": "pass",
            "findings": [],
            "confidence": 0.83,
        },
    }

    def fake_run(command, **kwargs):
        del kwargs
        agent_id = command[command.index("--agent") + 1]
        barrier.wait(timeout=2)
        return SimpleNamespace(
            returncode=0,
            stdout=json.dumps(payloads[agent_id]),
        )

    with patch("converge_orchestrator.opencode.run", side_effect=fake_run) as runner:
        result = adapter.invoke("reviewer", "Review this diff", cfg.repo_path)

    assert result.ok
    assert runner.call_count == 3
    aggregate = ReviewResult.model_validate_json(result.output)
    assert aggregate.verdict == "reject"
    assert aggregate.confidence == 0.83
    assert aggregate.reviewers == {
        "correctness_reviewer": "pass",
        "architecture_reviewer": "reject",
        "security_reviewer": "pass",
    }
    assert len(aggregate.findings) == 1
    assert aggregate.findings[0].reviewer == "architecture_reviewer"


def test_failed_review_process_becomes_deterministic_rejection(tmp_path: Path) -> None:
    cfg = _config(tmp_path)
    adapter = OpenCodeAdapter(cfg)

    def fake_run(command, **kwargs):
        del kwargs
        agent_id = command[command.index("--agent") + 1]
        if agent_id == "converge-security-reviewer":
            return SimpleNamespace(returncode=2, stdout="security model unavailable")
        return SimpleNamespace(
            returncode=0,
            stdout=json.dumps({"verdict": "pass", "findings": [], "confidence": 0.9}),
        )

    with patch("converge_orchestrator.opencode.run", side_effect=fake_run):
        result = adapter.invoke("reviewer", "Review this diff", cfg.repo_path)

    aggregate = ReviewResult.model_validate_json(result.output)
    assert aggregate.verdict == "reject"
    assert aggregate.reviewers["security_reviewer"] == "reject"
    security_findings = [
        finding
        for finding in aggregate.findings
        if finding.reviewer == "security_reviewer"
    ]
    assert len(security_findings) == 1
    assert "execution failed" in security_findings[0].reason
