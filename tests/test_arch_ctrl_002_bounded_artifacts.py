"""Lock ARCH-CTRL-002: controller-mediated bounded role artifacts.

Deterministic offline tests prove model-backed roles exchange information only through
controller-composed bounded artifacts (fresh sessions; bounded prompt inputs; no cross-role
narrative leakage), anchoring the verifier to
CONVERGE_ORCHESTRATOR_2_0_SOURCE_OF_TRUTH.md:L240.

No network, model, provider, git command, or subprocess call is made.
"""
from __future__ import annotations

import inspect
import json
import types
from pathlib import Path
from unittest.mock import patch

import pytest

from converge_orchestrator.models import (
    AgentConfig,
    ComplianceEntry,
    ComplianceSnapshot,
    ModelProfile,
    ProjectConfig,
    Requirement,
    RequirementStatus,
    ReviewFinding,
    ReviewResult,
    TaskEnvelope,
)
from converge_orchestrator.opencode import OpenCodeAdapter
from converge_orchestrator.prompts import builder_prompt, repair_prompt, reviewer_prompt

_BOUNDED_PROMPT_SIGNATURES: dict[str, set[str]] = {
    "builder_prompt": {"task", "requirements", "red_evidence"},
    "reviewer_prompt": {"task", "diff_text", "requirements", "compliance"},
    "repair_prompt": {"task", "requirements", "quality", "review"},
}

_PROHIBITED_NARRATIVE_MARKERS: tuple[str, ...] = (
    "Scout snapshot", "repo_scout", "working-memory", "working memory",
    "last_review_findings", "Builder said", "Builder reported", "Builder output",
    "Raw builder", "raw builder", "builder's raw output", "prior role output",
)


def _config(
    tmp_path: Path,
    *,
    review_roles: list[str] | None = None,
    context_tokens: int = 6000,
) -> ProjectConfig:
    repo = tmp_path / "repo"
    repo.mkdir(exist_ok=True)
    requirements = tmp_path / "architecture.md"
    requirements.write_text("System must remain correct.\n", encoding="utf-8")

    agents: dict[str, AgentConfig] = {
        "planner": AgentConfig(agent="converge-planner", model_profile="default"),
        "builder": AgentConfig(agent="converge-builder", model_profile="default"),
        "reviewer": AgentConfig(agent="converge-reviewer", model_profile="default"),
    }
    for role in review_roles or []:
        agent_id = f"converge-{role.replace('_', '-')}"
        agents[role] = AgentConfig(agent=agent_id, model_profile="default")

    return ProjectConfig(
        repo_path=repo,
        requirements_path=requirements,
        require_spec_read_only=False,
        context_input_fraction=0.5,
        context_output_reserve_tokens=1000,
        review_roles=review_roles or [],
        max_parallel_reviews=2,
        model_profiles={
            "default": ModelProfile(model="openai/test-model", context_tokens=context_tokens)
        },
        agents=agents,
    )


def _requirements() -> list[Requirement]:
    return [
        Requirement(
            id="ARCH-CTRL-002",
            statement=(
                "Model-backed roles SHALL exchange information through controller-mediated "
                "bounded artifacts rather than durable direct agent-to-agent chat."
            ),
            source="CONVERGE_ORCHESTRATOR_2_0_SOURCE_OF_TRUTH.md:L240",
        ),
        Requirement(
            id="ARCH-CTRL-001", statement="LangGraph durable state machine; Converge determinism.",
            source="CONVERGE_ORCHESTRATOR_2_0_SOURCE_OF_TRUTH.md:L239",
        ),
        Requirement(
            id="ARCH-CTRL-003", statement="Future convergence requirement pending.",
            source="CONVERGE_ORCHESTRATOR_2_0_SOURCE_OF_TRUTH.md:L300",
        ),
    ]


def _task() -> TaskEnvelope:
    return TaskEnvelope(
        id="ARCH-CTRL-002-0038",
        requirement_ids=["ARCH-CTRL-002"],
        title="Lock controller-mediated bounded role artifacts",
        objective=(
            "Add one deterministic offline test module that proves model-backed roles "
            "exchange information only through controller-composed bounded artifacts."
        ),
        constraints=[
            "Do not change production code or public API",
            "Do not weaken, delete, or skip existing tests",
            "Keep all changes inside tests/",
        ],
        allowed_paths=["tests/**"],
        acceptance=["The new test module passes under the configured pytest gate"],
        change_kind="test_only",
    )


def _assert_fresh_session(command: list[str], context: dict) -> None:
    assert "--continue" not in command
    assert "--session" not in command
    assert context.get("session_mode") == "fresh"


def _assert_no_prohibited_narrative(prompt: str) -> None:
    lowered = prompt.lower()
    assert not any(marker.lower() in lowered for marker in _PROHIBITED_NARRATIVE_MARKERS)


def test_prompt_builder_signatures_accept_only_bounded_inputs() -> None:
    """Structural guard: the prompt builders have no channel for cross-role raw narrative."""
    for name, expected in _BOUNDED_PROMPT_SIGNATURES.items():
        func = globals()[name]
        assert set(inspect.signature(func).parameters) == expected


@pytest.mark.parametrize("role", ["planner", "builder", "reviewer"])
def test_role_invocation_uses_fresh_session(tmp_path: Path, role: str) -> None:
    cfg = _config(tmp_path)
    adapter = OpenCodeAdapter(cfg)
    fake_result = types.SimpleNamespace(returncode=0, stdout="ok")

    target = "converge_orchestrator.opencode.ExecutionSandbox.run"
    with patch(target, return_value=fake_result) as runner:
        result = adapter.invoke(role, f"{role} prompt", cfg.repo_path)

    assert result.ok
    command = runner.call_args.args[0]
    _assert_fresh_session(command, result.context or {})
    assert runner.call_args.kwargs["include_state"] is False


def test_reviewer_fanout_uses_fresh_sessions_per_lane(tmp_path: Path) -> None:
    cfg = _config(tmp_path, review_roles=["architecture_reviewer", "correctness_reviewer"])
    adapter = OpenCodeAdapter(cfg)

    review_payload = ReviewResult(verdict="pass").model_dump_json()
    fake_stdout = json.dumps({"type": "text", "part": {"type": "text", "text": review_payload}})
    fake_result = types.SimpleNamespace(returncode=0, stdout=fake_stdout)

    target = "converge_orchestrator.opencode.ExecutionSandbox.run"
    with patch(target, return_value=fake_result) as runner:
        result = adapter.invoke("reviewer", "review prompt", cfg.repo_path)

    assert result.ok
    context = result.context or {}
    lanes = context.get("review_lanes", {})
    assert set(lanes) == {"architecture_reviewer", "correctness_reviewer"}

    # Fanout order is nondeterministic, so map each call by its dispatched agent id.
    calls_by_agent = {}
    for call in runner.call_args_list:
        command = call.args[0]
        kwargs = call.kwargs
        agent_id = command[command.index("--agent") + 1]
        calls_by_agent[agent_id] = (command, kwargs)

    for role in cfg.review_roles:
        agent_id = cfg.agents[role].agent
        command, kwargs = calls_by_agent[agent_id]
        lane_context = lanes.get(role, {})
        _assert_fresh_session(command, lane_context)
        assert kwargs["include_state"] is False


def test_builder_prompt_composed_only_from_envelope_and_target_requirements() -> None:
    task = _task()
    requirements = _requirements()
    prompt = builder_prompt(task, requirements)

    assert task.id in prompt
    assert task.objective in prompt
    assert task.title in prompt
    assert "ARCH-CTRL-002 | CONVERGE_ORCHESTRATOR_2_0_SOURCE_OF_TRUTH.md:L240" in prompt
    assert "Model-backed roles SHALL exchange information" in prompt
    for requirement in requirements:
        if requirement.id not in task.requirement_ids:
            assert requirement.id not in prompt
            assert requirement.statement not in prompt

    # Builder input must stay a pure bounded artifact.
    _assert_no_prohibited_narrative(prompt)


def test_reviewer_prompt_composed_only_from_envelope_scope_and_diff() -> None:
    task = _task()
    requirements = _requirements()
    diff_text = "@@ tests/test_arch_ctrl_002_bounded_artifacts.py\n+def test_fresh_session(): ..."
    compliance = ComplianceSnapshot(
        entries={
            "ARCH-CTRL-001": ComplianceEntry(
                requirement_id="ARCH-CTRL-001",
                status=RequirementStatus.PASS,
                evidence=["verified elsewhere"],
            )
        }
    )
    prompt = reviewer_prompt(task, diff_text, requirements, compliance)

    assert task.id in prompt
    assert task.objective in prompt
    assert task.title in prompt
    assert "REVIEW SCOPE (deterministic" in prompt
    assert diff_text in prompt

    boundaries = [
        "CURRENT TARGET REQUIREMENTS (mandatory",
        "ALREADY-SATISFIED REQUIREMENTS - NON-REGRESSION CONTRACT",
        "FUTURE / PENDING REQUIREMENTS - AUTHORITATIVE ROADMAP CONTEXT",
    ]
    starts = [prompt.index(header) for header in boundaries]
    assert starts[0] < starts[1] < starts[2]
    target_section = prompt[starts[0]:starts[1]]
    nonreg_section = prompt[starts[1]:starts[2]]
    future_section = prompt[starts[2]:]

    assert "ARCH-CTRL-002" in target_section
    assert "Model-backed roles SHALL exchange information" in target_section
    assert "ARCH-CTRL-001" in nonreg_section
    assert "ARCH-CTRL-003" in future_section

    # Reviewer input never carries Builder raw narrative or working memory.
    _assert_no_prohibited_narrative(prompt)


def test_repair_prompt_receives_only_controller_produced_artifacts() -> None:
    task = _task()
    requirements = _requirements()
    quality = [
        {"name": "pytest", "ok": False, "required": True, "returncode": 1, "output": "1 failed"}
    ]

    # The repair input is the controller-normalized review artifact (verdict + findings).
    review = ReviewResult(
        verdict="reject",
        findings=[
            ReviewFinding(
                severity="major",
                reason="Missing deterministic ARCH-CTRL-002 verifier",
                required_fix="Add the bounded-artifact contract test",
                requirement_id="ARCH-CTRL-002",
                reviewer="reviewer",
            )
        ],
        reviewers={"reviewer": "reject"},
    ).model_dump(mode="json")

    prompt = repair_prompt(task, requirements, quality, review)

    assert task.id in prompt
    assert task.objective in prompt
    assert "ARCH-CTRL-002" in prompt
    assert "Model-backed roles SHALL exchange information" in prompt
    assert json.dumps(quality, ensure_ascii=False) in prompt
    assert json.dumps(review, ensure_ascii=False) in prompt
    assert "ARCH-CTRL-001" not in prompt
    assert "ARCH-CTRL-003" not in prompt

    # The repair prompt never carries raw prior-role narrative from another role.
    _assert_no_prohibited_narrative(prompt)
