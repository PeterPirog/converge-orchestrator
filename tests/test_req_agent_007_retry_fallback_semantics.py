"""Pin REQ-AGENT-007: retry/fallback preserves role semantics and malformed
semantic output is never reclassified as transport failure.

REQ-AGENT-007 (CONVERGE_ORCHESTRATOR_2_0_SOURCE_OF_TRUTH.md:L255
[7. Deterministic architecture and agent roles]): Retry or fallback SHALL preserve role
instructions, permissions, target requirement, repository access, and authoritative context
semantics; malformed semantic output SHALL NOT be reclassified as transport failure.

These deterministic offline assertions lock the behavior:

* A fallback attempt after a failed primary uses the exact same ``--agent`` role id, the
  exact same stdin prompt bytes, the exact same cwd, and the exact same runtime permission
  configuration as the primary. No ``--continue`` or ``--session`` flags are supplied.
* A Planner reply that completed successfully (``ok=True``) but carries an invalid Task
  Envelope is classified as a semantic contract failure: it increments the Planner attempt
  counter, records ``last_failure_kind="contract"``, never touches the provider-recovery
  budget, and routes to ``planner_retry`` (not ``planner_provider_retry`` /
  ``planner_provider_stopped``).
* A completed review lane that returns malformed JSON is not a transport failure: the lane
  context contains no ``provider_failure_class``, and ``workflow.review()`` sets
  ``review_transport_failure`` semantics to ``False`` so the run routes to semantic repair
  instead of ``reviewer_recovery``.
"""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import patch

from converge_orchestrator.models import (
    AgentConfig,
    ComplianceEntry,
    ComplianceSnapshot,
    ModelGatewayConfig,
    ModelProfile,
    ProjectConfig,
    Requirement,
    RequirementStatus,
    ReviewResult,
    TaskEnvelope,
)
from converge_orchestrator.opencode import OpenCodeAdapter
from converge_orchestrator.risk import RiskReport
from converge_orchestrator.spec import sha256_file
from converge_orchestrator.targeting import route_after_targeted_plan, targeted_plan
from converge_orchestrator.workflow import review, route_after_review

_TARGET_ID = "REQ-AGENT-007"
_SOURCE_ANCHOR = "CONVERGE_ORCHESTRATOR_2_0_SOURCE_OF_TRUTH.md:255"

_REVIEW_ROLE = "correctness_reviewer"
_REVIEW_AGENT_ID = "converge-correctness-reviewer"


def _planner_config(tmp_path: Path, *, provider_retries: int = 0) -> ProjectConfig:
    repo = tmp_path / "repo"
    repo.mkdir()
    requirements = tmp_path / "architecture.md"
    requirements.write_text("System must preserve role semantics.\n", encoding="utf-8")
    return ProjectConfig(
        repo_path=repo,
        requirements_path=requirements,
        require_spec_read_only=False,
        context_output_reserve_tokens=256,
        model_gateway=ModelGatewayConfig(
            kind="openwebui",
            base_url="http://127.0.0.1:3000/api",
        ),
        model_profiles={
            "primary": ModelProfile(
                model="primary-model",
                context_tokens=32000,
                request_body={"temperature": 0.2},
            ),
            "fallback": ModelProfile(
                model="fallback-model",
                context_tokens=32000,
                request_body={"temperature": 0.0},
            ),
        },
        agents={
            "planner": AgentConfig(
                agent="converge-planner",
                model_profile="primary",
                fallback_model_profiles=["fallback"],
                provider_retries=provider_retries,
            )
        },
    )


def _targeting_config(tmp_path: Path) -> ProjectConfig:
    repo = tmp_path / "repo"
    repo.mkdir(exist_ok=True)
    requirements = tmp_path / "architecture.md"
    requirements.write_text("System must preserve role semantics.\n", encoding="utf-8")
    return ProjectConfig(
        repo_path=repo,
        requirements_path=requirements,
        state_dir=tmp_path / "state",
        worktree_dir=tmp_path / "state" / "worktrees",
        agents={"planner": AgentConfig(agent="converge-planner", model="openai/planner")},
        require_spec_read_only=False,
    )


def _targeting_state(tmp_path: Path) -> dict:
    return {
        "config_path": str(tmp_path / "converge.yaml"),
        "run_id": "run-1",
        "requirements_hash": "spec-sha",
        "requirements": [
            Requirement(
                id=_TARGET_ID,
                statement="Retry or fallback must preserve role semantics.",
                source=_SOURCE_ANCHOR,
            ).model_dump(mode="json")
        ],
        "compliance": ComplianceSnapshot(
            entries={
                _TARGET_ID: ComplianceEntry(
                    requirement_id=_TARGET_ID,
                    status=RequirementStatus.FAIL,
                )
            }
        ).model_dump(mode="json"),
        "baseline": {"repo_scout": {"base_commit": "base-sha"}},
        "iteration": 0,
        "replan_attempts": 0,
    }


def _agent_result(*, ok: bool, output: str, context: dict | None = None) -> SimpleNamespace:
    payload: dict = {"budget_status": "bounded"}
    if context is not None:
        payload.update(context)
    return SimpleNamespace(ok=ok, output=output, context=payload)


def _review_config(tmp_path: Path) -> ProjectConfig:
    repo = tmp_path / "repo"
    repo.mkdir(exist_ok=True)
    requirements = tmp_path / "architecture.md"
    requirements.write_text("System must preserve role semantics.\n", encoding="utf-8")
    state_dir = tmp_path / "state"
    state_dir.mkdir(exist_ok=True)
    return ProjectConfig(
        repo_path=repo,
        requirements_path=requirements,
        state_dir=state_dir,
        worktree_dir=state_dir / "worktrees",
        base_branch="main",
        review_roles=[_REVIEW_ROLE],
        max_parallel_reviews=1,
        agents={
            _REVIEW_ROLE: AgentConfig(agent=_REVIEW_AGENT_ID, model="openai/reviewer"),
        },
        require_spec_read_only=False,
    )


def _review_state(cfg: ProjectConfig) -> dict:
    task = TaskEnvelope(
        id="task-1",
        requirement_ids=[_TARGET_ID],
        title="Role semantic pin",
        objective="Prove malformed semantic output is never transport failure.",
    )
    return {
        "config_path": str(cfg.repo_path / "converge.yaml"),
        "run_id": "run-1",
        "requirements_hash": sha256_file(cfg.requirements_path),
        "requirements": [
            Requirement(
                id=_TARGET_ID,
                statement="Malformed semantic output must stay semantic.",
                source=_SOURCE_ANCHOR,
            ).model_dump(mode="json")
        ],
        "compliance": ComplianceSnapshot().model_dump(mode="json"),
        "baseline": {},
        "iteration": 0,
        "task": task.model_dump(mode="json"),
        "worktree": str(cfg.worktree_dir),
        "repair_attempts": 0,
        "replan_attempts": 0,
        "review_execution_retries": 0,
    }


def test_fallback_reuses_identical_role_prompt_cwd_and_permissions(tmp_path: Path) -> None:
    """A fallback attempt is identical to the primary in role, prompt, cwd, and permissions."""
    cfg = _planner_config(tmp_path)
    adapter = OpenCodeAdapter(cfg)
    responses = [
        SimpleNamespace(returncode=1, stdout="provider unavailable"),
        SimpleNamespace(returncode=0, stdout='{"task": "ok"}'),
    ]

    target = "converge_orchestrator.opencode.ExecutionSandbox.run"
    prompt = "Plan one immutable target with authoritative context"
    with (
        patch(target, side_effect=responses) as runner,
        patch("converge_orchestrator.opencode._sleep"),
    ):
        result = adapter.invoke("planner", prompt, cfg.repo_path)

    assert result.ok is True
    assert runner.call_count == 2

    calls = runner.call_args_list
    commands = [call.args[0] for call in calls]
    agent_ids = [command[command.index("--agent") + 1] for command in commands]
    assert agent_ids == ["converge-planner", "converge-planner"]
    assert all("--continue" not in command and "--session" not in command for command in commands)

    inputs = [call.kwargs["input"] for call in calls]
    assert inputs[0] == inputs[1]
    assert inputs[0] == prompt

    cwds = [call.kwargs["cwd"] for call in calls]
    assert cwds[0] == cwds[1] == cfg.repo_path

    runtime_payloads = [
        json.loads(call.kwargs["env"]["OPENCODE_CONFIG_CONTENT"]) for call in calls
    ]
    primary = runtime_payloads[0]["agent"]["converge-planner"]
    fallback = runtime_payloads[1]["agent"]["converge-planner"]
    assert fallback["permission"] == primary["permission"]
    assert fallback["model"] == "openwebui/fallback-model"
    assert primary["model"] == "openwebui/primary-model"

    attempts = cast(dict, result.context)["provider_attempts"]
    assert [(item["model_profile"], item["ok"]) for item in attempts] == [
        ("primary", False),
        ("fallback", True),
    ]


def test_malformed_planner_envelope_is_contract_not_transport(tmp_path: Path) -> None:
    """A completed Planner session with invalid JSON consumes semantic
    attempts, not provider budget."""
    cfg = _targeting_config(tmp_path)
    state = _targeting_state(tmp_path)

    with (
        patch("converge_orchestrator.targeting.load_config", return_value=cfg),
        patch("converge_orchestrator.workflow.load_config", return_value=cfg),
        patch("converge_orchestrator.targeting.wf._write_compliance"),
        patch(
            "converge_orchestrator.targeting.OpenCodeAdapter.invoke",
            return_value=_agent_result(ok=True, output="not-json"),
        ),
    ):
        result: dict[str, Any] = targeted_plan(state)  # type: ignore[arg-type]

    assert result["status"] == "planner_retry"
    assert route_after_targeted_plan(cast(Any, result)) == "retry"
    assert result["task"] is None

    control = result["baseline"]["planner_control"]
    assert control["target_requirement_id"] == _TARGET_ID
    assert control["attempts"] == 1
    assert control["last_failure_kind"] == "contract"
    assert control["provider_recovery_attempts"] == 0
    assert result["status"] not in {"planner_provider_retry", "planner_provider_stopped"}


def test_malformed_review_lane_ok_is_semantic_not_transport(tmp_path: Path) -> None:
    """A completed review lane with invalid JSON is a semantic reject, never transport."""
    cfg = _review_config(tmp_path)
    adapter = OpenCodeAdapter(cfg)

    def fake_run(command, **kwargs):
        del kwargs
        agent_id = command[command.index("--agent") + 1]
        assert agent_id == _REVIEW_AGENT_ID
        return SimpleNamespace(returncode=0, stdout="not valid json")

    target = "converge_orchestrator.opencode.ExecutionSandbox.run"
    with (
        patch(target, side_effect=fake_run) as runner,
        patch("converge_orchestrator.opencode._sleep"),
    ):
        result = adapter.invoke("reviewer", "Review this diff", cast(Path, cfg.worktree_dir))

    assert result.ok is True
    assert runner.call_count == 1
    aggregate = ReviewResult.model_validate_json(result.output)
    assert aggregate.verdict == "reject"
    assert aggregate.reviewers == {_REVIEW_ROLE: "reject"}

    result_context = cast(dict, result.context)
    lane_context = result_context["review_lanes"][_REVIEW_ROLE]
    assert "provider_failure_class" not in lane_context

    state = _review_state(cfg)
    with (
        patch("converge_orchestrator.workflow.load_config", return_value=cfg),
        patch("converge_orchestrator.workflow.diff", return_value="patch text"),
        patch(
            "converge_orchestrator.workflow.classify_repository_risk",
            return_value=RiskReport(findings=[], flags=[]),
        ),
        patch(
            "converge_orchestrator.workflow.OpenCodeAdapter.invoke",
            return_value=result,
        ),
    ):
        reviewed: dict[str, Any] = review(state)  # type: ignore[arg-type]

        assert reviewed["status"] == "reviewed"
        assert reviewed.get("review_execution_retries") == 0
        assert reviewed.get("status") != "review_transport_failure"
        route = route_after_review(cast(Any, reviewed))
        assert route != "reviewer_recovery"
        assert route in {"repair", "replan", "human"}
