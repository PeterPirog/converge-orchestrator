"""Pin REQ-AGENT-013: no hidden conversational state across model invocations.

REQ-AGENT-013 (CONVERGE_ORCHESTRATOR_2_0_SOURCE_OF_TRUTH.md:L261
[7. Deterministic architecture and agent roles]): Each model-backed role invocation SHALL begin
without hidden conversational state inherited from an earlier invocation, and all
cross-invocation continuity needed for correctness SHALL be reconstructed from
controller-selected durable state, pinned context, and evidence rather than provider-side
session memory.

This deterministic offline regression module locks the invariants:

* Every OpenCode process launch for a model-backed role uses ``session_mode=fresh``,
  never passes ``--continue`` or ``--session``, and never mounts the orchestrator state
  directory into the agent runtime (``include_state=False``). This holds for the
  primary attempt, provider retries, and every lane of a reviewer fan-out.
* Cross-invocation continuity is produced deterministically from the passed
  ``WorkflowState``: ``build_working_memory`` is a pure function of state, the Planner
  receives it only as explicitly-labeled ADVISORY sections, and the repository-scout
  snapshot is read from persisted ``baseline.repo_scout`` evidence rather than from any
  provider chat history.
"""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import patch

import pytest

from converge_orchestrator.context import (
    AdvisorySection,
    PromptEnvelope,
    build_working_memory,
    prepare_prompt,
)
from converge_orchestrator.models import (
    AgentConfig,
    AgentResult,
    ComplianceSnapshot,
    ModelGatewayConfig,
    ModelProfile,
    ProjectConfig,
    Requirement,
)
from converge_orchestrator.opencode import OpenCodeAdapter

_TARGET_ID = "REQ-AGENT-013"
_SOURCE_ANCHOR = "CONVERGE_ORCHESTRATOR_2_0_SOURCE_OF_TRUTH.md:L261"


def _config(tmp_path: Path, *, provider_retries: int = 0) -> ProjectConfig:
    repo = tmp_path / "repo"
    repo.mkdir(exist_ok=True)
    requirements = tmp_path / "architecture.md"
    requirements.write_text("System must start every model invocation fresh.\n", encoding="utf-8")
    return ProjectConfig(
        repo_path=repo,
        requirements_path=requirements,
        require_spec_read_only=False,
        context_output_reserve_tokens=256,
        max_parallel_reviews=2,
        review_roles=["architecture_reviewer", "correctness_reviewer"],
        model_gateway=ModelGatewayConfig(
            kind="openwebui", base_url="http://127.0.0.1:3000/api"
        ),
        model_profiles={
            "planner": ModelProfile(model="planner-model", context_tokens=32000),
            "builder": ModelProfile(model="builder-model", context_tokens=32000),
            "reviewer": ModelProfile(model="reviewer-model", context_tokens=32000),
            "fallback": ModelProfile(model="fallback-model", context_tokens=32000),
        },
        agents={
            "planner": AgentConfig(
                agent="converge-planner",
                model_profile="planner",
                provider_retries=provider_retries,
            ),
            "builder": AgentConfig(
                agent="converge-builder",
                model_profile="builder",
                provider_retries=provider_retries,
            ),
            "architecture_reviewer": AgentConfig(
                agent="converge-architecture-reviewer",
                model_profile="reviewer",
            ),
            "correctness_reviewer": AgentConfig(
                agent="converge-correctness-reviewer",
                model_profile="reviewer",
                fallback_model_profiles=["fallback"],
            ),
        },
    )


def _assert_fresh_call(call: Any, *, role: str) -> None:
    command = call.args[0]
    assert "--continue" not in command, f"{role} leaked a --continue session flag"
    assert "--session" not in command, f"{role} leaked a --session flag"
    kwargs = call.kwargs
    assert kwargs["include_state"] is False, f"{role} mounted state into agent runtime"
    assert kwargs["agent_role"] == role
    assert kwargs["scope"] == "agent"


def test_adapter_invokes_planner_and_builder_with_fresh_sessions(
    tmp_path: Path,
) -> None:
    """Primary role invocations for planner and builder start fresh."""
    cfg = _config(tmp_path)
    adapter = OpenCodeAdapter(cfg)
    target = "converge_orchestrator.opencode.ExecutionSandbox.run"
    roles = ["planner", "builder"]

    with patch(target, return_value=SimpleNamespace(returncode=0, stdout="ok")) as runner:
        for role in roles:
            result = adapter.invoke(role, f"{role} prompt", cfg.repo_path)
            assert result.ok
            _assert_fresh_call(runner.call_args, role=role)
            assert result.context is not None
            assert result.context["session_mode"] == "fresh"

    assert runner.call_count == len(roles)


@pytest.mark.parametrize("role", ["planner", "builder"])
def test_adapter_provider_retries_are_fresh_sessions(role: str, tmp_path: Path) -> None:
    """Retry attempts use identical, fresh prompts with no provider session memory."""
    cfg = _config(tmp_path, provider_retries=2)
    adapter = OpenCodeAdapter(cfg)
    target = "converge_orchestrator.opencode.ExecutionSandbox.run"
    prompt = f"{role} prompt with durable state only"
    responses = [
        SimpleNamespace(returncode=1, stdout="provider blip"),
        SimpleNamespace(returncode=1, stdout="still blipping"),
        SimpleNamespace(returncode=0, stdout="ok"),
    ]

    with patch(target, side_effect=responses) as runner, patch(
        "converge_orchestrator.opencode._sleep"
    ):
        result = adapter.invoke(role, prompt, cfg.repo_path)

    assert result.ok
    assert runner.call_count == 3
    calls = runner.call_args_list
    inputs = [call.kwargs["input"] for call in calls]
    assert inputs == [prompt, prompt, prompt], "retry prompt bytes changed across attempts"
    for call in calls:
        _assert_fresh_call(call, role=role)
        assert call.kwargs["writable_cwd"] is (role == "builder")
    attempts = cast(dict, result.context)["provider_attempts"]
    assert [item["retry"] for item in attempts] == [0, 1, 2]


def test_adapter_review_fanout_lanes_are_fresh_sessions(tmp_path: Path) -> None:
    """Every lane of a reviewer fan-out starts from a fresh session."""
    cfg = _config(tmp_path)
    adapter = OpenCodeAdapter(cfg)
    target = "converge_orchestrator.opencode.ExecutionSandbox.run"

    def _fake_run(command: list[str], **kwargs: object) -> SimpleNamespace:
        return SimpleNamespace(returncode=0, stdout="ok")

    with patch(target, side_effect=_fake_run) as runner, patch(
        "converge_orchestrator.opencode._sleep"
    ):
        result = adapter.invoke("reviewer", "review this candidate", cfg.repo_path)

    assert result.ok
    calls = runner.call_args_list
    lanes = {call.kwargs["agent_role"] for call in calls}
    assert lanes == {"architecture_reviewer", "correctness_reviewer"}
    for call in calls:
        role = call.kwargs["agent_role"]
        _assert_fresh_call(call, role=role)
        assert call.kwargs["writable_cwd"] is False
    assert result.context is not None
    review_lanes = result.context.get("review_lanes", {})
    assert set(review_lanes) == lanes
    for lane_context in review_lanes.values():
        assert lane_context["session_mode"] == "fresh"


def test_build_working_memory_is_pure_state_function() -> None:
    """Working memory is a deterministic, state-only function with no external input."""
    base = {
        "requirements": [
            {
                "id": _TARGET_ID,
                "statement": "Hidden session memory must not cross invocations.",
                "source": _SOURCE_ANCHOR,
                "severity": "mandatory",
            }
        ],
        "compliance": {"entries": {_TARGET_ID: {"status": "fail", "evidence": []}}},
        "iteration": 1,
        "status": "replanning",
    }
    memory = build_working_memory(cast(Any, base))
    assert memory["source"] == "deterministic LangGraph state; advisory only"
    assert memory["immutable_requirements_remain_authoritative"] is True
    assert memory["iteration"] == 1
    assert _TARGET_ID in memory["unresolved_mandatory_requirement_ids"]

    changed = {**base, "iteration": 2, "status": "planned"}
    changed_memory = build_working_memory(cast(Any, changed))
    assert changed_memory["iteration"] == 2
    assert changed_memory["status"] == "planned"

    # The function never reads files, environment, or provider sessions.
    assert "statement" not in json.dumps(memory)


def test_graph_plan_prompt_carries_durable_advisory_continuity(tmp_path: Path) -> None:
    """Planner continuity is reconstructed from durable state and labeled advisory only."""
    cfg = _config(tmp_path)
    snapshot = {
        "source": "agent",
        "base_branch": "main",
        "base_commit": "abc123",
        "warnings": ["small repo"],
    }
    requirement = Requirement(
        id=_TARGET_ID,
        statement="Cross-invocation continuity must be durable.",
        source=_SOURCE_ANCHOR,
        severity="mandatory",
    )
    state = {
        "config_path": str(cfg.repo_path / "converge.yaml"),
        "run_id": "run-013",
        "requirements_hash": "spec-sha",
        "requirements": [requirement.model_dump(mode="json")],
        "compliance": ComplianceSnapshot().model_dump(mode="json"),
        "baseline": {"repo_scout": snapshot},
        "iteration": 0,
        "repair_attempts": 0,
        "replan_attempts": 0,
    }
    task_json = {
        "id": f"{_TARGET_ID}-001",
        "requirement_ids": [_TARGET_ID],
        "title": "Fresh invocation pin",
        "objective": "Prove no hidden state",
        "constraints": ["Do not change production code or public API"],
        "allowed_paths": ["tests/**"],
        "acceptance": ["New module passes"],
        "max_diff_lines": 400,
        "risk": "low",
        "risk_flags": [],
        "change_kind": "test_only",
        "tdd": {
            "mode": "not_applicable",
            "test_paths": [],
            "test_gate": None,
            "expected_failure_pattern": None,
            "rationale": "Regression test pin",
        },
    }
    captured: list[tuple[str, Any, Path]] = []

    def _capture_invoke(role: str, prompt: Any, cwd: Path) -> AgentResult:
        captured.append((role, prompt, cwd))
        return AgentResult(
            role=role,
            ok=True,
            output=json.dumps(task_json),
            returncode=0,
            context={"budget_status": "bounded", "session_mode": "fresh"},
        )

    from converge_orchestrator import graph

    with (
        patch("converge_orchestrator.graph.load_config", return_value=cfg),
        patch("converge_orchestrator.workflow.load_config", return_value=cfg),
        patch("converge_orchestrator.graph.update_base"),
        patch.object(OpenCodeAdapter, "invoke", side_effect=_capture_invoke),
    ):
        graph.plan(cast(Any, state))

    assert len(captured) == 1
    role, prompt, cwd = captured[0]
    assert role == "planner"
    assert cwd == cfg.repo_path
    assert isinstance(prompt, PromptEnvelope)
    section_names = [section.name for section in prompt.advisory]
    assert "repository scout snapshot" in section_names
    assert "working memory" in section_names

    working_section = next(s for s in prompt.advisory if s.name == "working memory")
    expected_memory = build_working_memory(cast(Any, state))
    assert working_section.text == json.dumps(expected_memory, ensure_ascii=False, indent=2)

    rendered, report = prepare_prompt(cfg, "planner", prompt)
    assert rendered.startswith(prompt.core)
    assert "WORKING MEMORY (ADVISORY;" in rendered
    assert "REPOSITORY SCOUT SNAPSHOT (ADVISORY;" in rendered
    assert report.session_mode == "fresh"

    state_dir = cast(Path, cfg.state_dir)
    memory_path = state_dir / "evidence" / "run-013" / "run" / "working-memory-0001.json"
    assert memory_path.is_file()
    persisted = json.loads(memory_path.read_text(encoding="utf-8"))
    assert persisted == expected_memory


def test_prompt_reaches_provider_as_fresh_stdin_without_session_flags(
    tmp_path: Path,
) -> None:
    """A PromptEnvelope prompt is delivered over stdin with no --continue/--session flags."""
    cfg = _config(tmp_path)
    adapter = OpenCodeAdapter(cfg)
    memory = build_working_memory(
        cast(
            Any,
            {
                "requirements": [],
                "compliance": {"entries": {}},
                "iteration": 3,
                "status": "planned",
            },
        )
    )
    prompt = PromptEnvelope(
        core="Planner core instructions",
        advisory=(
            AdvisorySection("repository scout snapshot", '{"base_commit":"abc"}'),
            AdvisorySection("working memory", json.dumps(memory, ensure_ascii=False)),
        ),
    )
    target = "converge_orchestrator.opencode.ExecutionSandbox.run"

    with patch(target, return_value=SimpleNamespace(returncode=0, stdout="ok")) as runner:
        result = adapter.invoke("planner", prompt, cfg.repo_path)

    assert result.ok
    _assert_fresh_call(runner.call_args, role="planner")
    command = runner.call_args.args[0]
    rendered = runner.call_args.kwargs["input"]
    assert rendered.startswith("Planner core instructions")
    assert "WORKING MEMORY (ADVISORY;" in rendered
    assert "REPOSITORY SCOUT SNAPSHOT (ADVISORY;" in rendered
    assert "--continue" not in command
    assert "--session" not in command
