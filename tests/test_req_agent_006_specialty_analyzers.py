from __future__ import annotations

import json
import pathlib
import types
from unittest.mock import patch

import pytest
from pydantic import ValidationError

from converge_orchestrator.models import ProjectConfig, ReviewResult
from converge_orchestrator.opencode import OpenCodeAdapter
from converge_orchestrator.opencode_config import (
    _SPECIALTY_READ_ONLY_DIRECTIVE,
    build_opencode_config,
)


SPECIALTY_DESCRIPTION = "Independent read-only performance and efficiency reviewer."
SPECIALTY_PROMPT = (
    "You are the performance review lane. Inspect the actual diff and surrounding code "
    "for algorithmic regressions, resource leaks, unnecessary work, and observable "
    "latency or throughput degradation. Stay read-only and report only evidence-backed "
    "findings. When requested for JSON, output JSON only."
)


BUILTIN_REVIEW_ROLES = [
    "correctness_reviewer",
    "architecture_reviewer",
    "security_reviewer",
]


def _base_config(tmp_path: pathlib.Path) -> dict:
    repo = tmp_path / "repo"
    repo.mkdir()
    requirements = tmp_path / "architecture.md"
    requirements.write_text("System must remain reviewable.\n", encoding="utf-8")
    return {
        "version": 1,
        "project": {
            "repo_path": str(repo),
            "requirements_path": str(requirements),
            "require_spec_read_only": False,
        },
        "workflow": {
            "review_roles": list(BUILTIN_REVIEW_ROLES),
            "max_parallel_reviews": 4,
        },
        "agents": {
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
    }


def _config_with_specialty(tmp_path: pathlib.Path) -> ProjectConfig:
    raw = _base_config(tmp_path)
    raw["specialty_analyzers"] = {
        "performance_reviewer": {
            "description": SPECIALTY_DESCRIPTION,
            "prompt": SPECIALTY_PROMPT,
        }
    }
    raw["workflow"]["review_roles"].append("performance_reviewer")
    raw["agents"]["performance_reviewer"] = {
        "agent": "converge-performance-reviewer",
        "model": "openai/performance",
    }
    return ProjectConfig.model_validate(raw)


def test_req_agent_006_specialty_analyzer_lane_is_declared_and_read_only(
    tmp_path: pathlib.Path,
) -> None:
    """REQ-AGENT-006: a declared specialty analyzer is accepted as a review lane and
    emitted with read-only permissions plus its declared read-only prompt."""
    cfg = _config_with_specialty(tmp_path)

    assert "performance_reviewer" in cfg.review_roles
    assert cfg.specialty_analyzers["performance_reviewer"].description == SPECIALTY_DESCRIPTION
    assert cfg.specialty_analyzers["performance_reviewer"].prompt == SPECIALTY_PROMPT

    payload = build_opencode_config(cfg)
    agent = payload["agent"]["converge-performance-reviewer"]

    assert agent["description"] == SPECIALTY_DESCRIPTION
    expected_prompt = f"{SPECIALTY_PROMPT}\n\n{_SPECIALTY_READ_ONLY_DIRECTIVE}"
    assert agent["prompt"] == expected_prompt
    assert agent["permission"]["edit"] == "deny"
    assert agent["permission"]["task"] == "deny"
    assert agent["permission"]["external_directory"] == "deny"
    assert agent["permission"]["bash"]["*"] == "deny"
    assert agent["permission"]["bash"]["git diff"] == "allow"


def test_req_agent_006_specialty_analyzer_count_capped_at_four(
    tmp_path: pathlib.Path,
) -> None:
    raw = _base_config(tmp_path)
    raw["specialty_analyzers"] = {
        f"analyzer_{i}": {"description": f"desc {i}", "prompt": f"prompt {i}"}
        for i in range(5)
    }
    with pytest.raises(ValidationError, match="at most 4 specialty analyzers"):
        ProjectConfig.model_validate(raw)


def test_req_agent_006_specialty_analyzer_rejects_blank_name(
    tmp_path: pathlib.Path,
) -> None:
    raw = _base_config(tmp_path)
    raw["specialty_analyzers"] = {
        "   ": {"description": "blank name", "prompt": "prompt"},
    }
    with pytest.raises(ValidationError, match="specialty analyzer name must not be blank"):
        ProjectConfig.model_validate(raw)


def test_req_agent_006_specialty_analyzer_rejects_builtin_collision(
    tmp_path: pathlib.Path,
) -> None:
    raw = _base_config(tmp_path)
    raw["specialty_analyzers"] = {
        "reviewer": {"description": "collides with built-in", "prompt": "prompt"},
    }
    with pytest.raises(ValidationError, match="reserved built-in role"):
        ProjectConfig.model_validate(raw)


def test_req_agent_006_specialty_lane_fanout_aggregates_and_blocks(
    tmp_path: pathlib.Path,
) -> None:
    cfg = _config_with_specialty(tmp_path)
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
            "verdict": "pass",
            "findings": [],
            "confidence": 0.83,
        },
        "converge-performance-reviewer": {
            "verdict": "reject",
            "findings": [
                {
                    "severity": "major",
                    "reason": "Observable performance regression in hot path.",
                    "requirement_id": "PERF-001",
                }
            ],
            "confidence": 0.85,
        },
    }

    def fake_run(command, **kwargs):
        agent_id = command[command.index("--agent") + 1]
        # Specialty lanes must never receive a writable working directory.
        if agent_id == "converge-performance-reviewer":
            assert kwargs.get("writable_cwd") is False
        return types.SimpleNamespace(
            returncode=0,
            stdout=json.dumps(payloads[agent_id]),
        )

    target = "converge_orchestrator.opencode.ExecutionSandbox.run"
    with patch(target, side_effect=fake_run) as runner:
        result = adapter.invoke("reviewer", "Review this diff", cfg.repo_path)

    assert result.ok
    assert runner.call_count == 4

    aggregate = ReviewResult.model_validate_json(result.output)
    assert aggregate.verdict == "reject"
    assert aggregate.reviewers == {
        "correctness_reviewer": "pass",
        "architecture_reviewer": "pass",
        "security_reviewer": "pass",
        "performance_reviewer": "reject",
    }
    performance_findings = [
        finding
        for finding in aggregate.findings
        if finding.reviewer == "performance_reviewer"
    ]
    assert len(performance_findings) == 1
    assert "performance regression" in performance_findings[0].reason.lower()


@pytest.mark.parametrize("reserved", ["builder", "scout", "planner"])
def test_req_agent_006_specialty_analyzer_rejects_non_review_builtin_collision(
    tmp_path: pathlib.Path, reserved: str
) -> None:
    """A specialty analyzer name must never collide with any built-in role (builder, scout,
    planner included); otherwise it would be reinterpreted as that built-in role and could
    regain write authority in the review fanout."""
    raw = _base_config(tmp_path)
    raw["specialty_analyzers"] = {
        reserved: {"description": "collides with built-in", "prompt": "prompt"},
    }
    with pytest.raises(ValidationError, match="reserved built-in role"):
        ProjectConfig.model_validate(raw)


@pytest.mark.parametrize(
    "bad_name",
    [
        "performance/reviewer",
        "..",
        "PerformanceReviewer",
        "performance-reviewer",
        "performance reviewer",
        "a" * 65,
    ],
)
def test_req_agent_006_specialty_analyzer_rejects_unsafe_identifier(
    tmp_path: pathlib.Path, bad_name: str
) -> None:
    raw = _base_config(tmp_path)
    raw["specialty_analyzers"] = {bad_name: {"description": "desc", "prompt": "prompt"}}
    with pytest.raises(ValidationError, match="specialty analyzer name"):
        ProjectConfig.model_validate(raw)


@pytest.mark.parametrize("field", ["description", "prompt"])
def test_req_agent_006_specialty_analyzer_rejects_blank_field(
    tmp_path: pathlib.Path, field: str
) -> None:
    raw = _base_config(tmp_path)
    raw["specialty_analyzers"] = {
        "performance_reviewer": {"description": "desc", "prompt": "prompt", field: "   "}
    }
    with pytest.raises(ValidationError, match=f"specialty analyzer {field} must not be blank"):
        ProjectConfig.model_validate(raw)