import json
import pathlib
import threading
import types
import unittest.mock

from converge_orchestrator.models import AgentResult, ProjectConfig, ReviewResult
from converge_orchestrator.opencode import OpenCodeAdapter, _normalize_review
from converge_orchestrator.opencode_config import build_opencode_config

REVIEW_BODY = json.dumps(
    {
        "verdict": "reject",
        "findings": [
            {
                "severity": "blocker",
                "requirement_id": "REQ-F92FFC55BA",
                "file": "shared_tools/fake_terminal.py",
                "line": 11,
                "reason": "Target deliverable is absent: no public simulate_command() exists.",
                "required_fix": "Implement the additive public simulate_command.",
            }
        ],
        "confidence": 0.97,
    },
    indent=2,
)

# Faithful minimized structural reproduction of the captured V26 failure: the lane
# quotes several OpenCode permission-rule JSON objects before delivering one valid
# ReviewResult in the same semantic output.
FALSE_REJECT_TEXT = "\n".join(
    [
        "Let me examine the diff.",
        "The permission rules look odd:",
        json.dumps({"permission": "*", "action": "allow", "pattern": "*"}),
        json.dumps({"permission": "*", "action": "deny", "pattern": "*"}),
        json.dumps({"permission": "bash", "pattern": "*", "action": "deny"}),
        "Continuing the review...",
        "```json",
        REVIEW_BODY,
        "```",
    ]
)

REVIEW_ROLES = [
    "correctness_reviewer",
    "architecture_reviewer",
    "security_reviewer",
]


def _config(tmp_path: pathlib.Path) -> ProjectConfig:
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


def test_parallel_review_uses_read_only_specialized_agents(tmp_path: pathlib.Path) -> None:
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


def test_parallel_review_runs_concurrently_and_one_reject_blocks(
    tmp_path: pathlib.Path,
) -> None:
    cfg = _config(tmp_path)
    adapter = OpenCodeAdapter(cfg)
    barrier = threading.Barrier(3)
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
        return types.SimpleNamespace(
            returncode=0,
            stdout=json.dumps(payloads[agent_id]),
        )

    target = "converge_orchestrator.opencode.ExecutionSandbox.run"
    with unittest.mock.patch(target, side_effect=fake_run) as runner:
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


def test_failed_review_process_becomes_deterministic_rejection(
    tmp_path: pathlib.Path,
) -> None:
    cfg = _config(tmp_path)
    adapter = OpenCodeAdapter(cfg)

    def fake_run(command, **kwargs):
        del kwargs
        agent_id = command[command.index("--agent") + 1]
        if agent_id == "converge-security-reviewer":
            return types.SimpleNamespace(
                returncode=2,
                stdout="security model unavailable",
            )
        return types.SimpleNamespace(
            returncode=0,
            stdout=json.dumps({"verdict": "pass", "findings": [], "confidence": 0.9}),
        )

    target = "converge_orchestrator.opencode.ExecutionSandbox.run"
    with unittest.mock.patch(target, side_effect=fake_run):
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


def test_normalize_review_attributes_lane_role_and_preserves_explicit_reviewer() -> None:
    lane = "security_reviewer"
    review = _normalize_review(
        lane,
        AgentResult(role=lane, ok=True, output=REVIEW_BODY),
    )
    assert review.findings[0].reviewer == lane
    assert review.reviewers == {lane: "reject"}

    explicit = json.dumps(
        {
            "verdict": "pass",
            "findings": [
                {
                    "severity": "note",
                    "reason": "Minor observation.",
                    "reviewer": "reviewer",
                }
            ],
        }
    )
    attributed = _normalize_review(
        lane,
        AgentResult(role=lane, ok=True, output=explicit),
    )
    assert attributed.findings[0].reviewer == "reviewer"
    assert attributed.reviewers == {lane: "pass"}


def test_mixed_lane_output_with_permission_decoys_returns_real_reject(
    tmp_path: pathlib.Path,
) -> None:
    cfg = _config(tmp_path)
    adapter = OpenCodeAdapter(cfg)

    def fake_run(command, **kwargs):
        del kwargs
        agent_id = command[command.index("--agent") + 1]
        if agent_id == "converge-security-reviewer":
            return types.SimpleNamespace(returncode=0, stdout=FALSE_REJECT_TEXT)
        return types.SimpleNamespace(
            returncode=0,
            stdout=json.dumps({"verdict": "pass", "findings": [], "confidence": 0.9}),
        )

    target = "converge_orchestrator.opencode.ExecutionSandbox.run"
    with unittest.mock.patch(target, side_effect=fake_run):
        result = adapter.invoke("reviewer", "Review this diff", cfg.repo_path)

    aggregate = ReviewResult.model_validate_json(result.output)
    assert aggregate.verdict == "reject"
    assert aggregate.reviewers == {
        "correctness_reviewer": "pass",
        "architecture_reviewer": "pass",
        "security_reviewer": "reject",
    }
    security_findings = [
        finding
        for finding in aggregate.findings
        if finding.reviewer == "security_reviewer"
    ]
    assert len(security_findings) == 1
    assert "Target deliverable" in security_findings[0].reason
    assert "returned invalid review JSON" not in security_findings[0].reason


def test_malformed_lane_becomes_attributed_reject_and_is_never_dropped(
    tmp_path: pathlib.Path,
) -> None:
    cfg = _config(tmp_path)
    adapter = OpenCodeAdapter(cfg)

    def fake_run(command, **kwargs):
        del kwargs
        agent_id = command[command.index("--agent") + 1]
        if agent_id == "converge-architecture-reviewer":
            # Lane emitted only an unrelated permission-event object; no verdict anywhere.
            return types.SimpleNamespace(
                returncode=0,
                stdout=json.dumps(
                    {"permission": "*", "action": "deny", "pattern": "*"}
                ),
            )
        return types.SimpleNamespace(
            returncode=0,
            stdout=json.dumps({"verdict": "pass", "findings": [], "confidence": 0.9}),
        )

    target = "converge_orchestrator.opencode.ExecutionSandbox.run"
    with unittest.mock.patch(target, side_effect=fake_run):
        result = adapter.invoke("reviewer", "Review this diff", cfg.repo_path)

    aggregate = ReviewResult.model_validate_json(result.output)
    assert aggregate.verdict == "reject"
    assert aggregate.reviewers["architecture_reviewer"] == "reject"
    architecture_findings = [
        finding
        for finding in aggregate.findings
        if finding.reviewer == "architecture_reviewer"
    ]
    assert len(architecture_findings) == 1
    assert "returned invalid review JSON" in architecture_findings[0].reason
