"""Pin REQ-CONTEXT-001: exact target in core, advisory scout memory in prompt."""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import Mock, patch

from converge_orchestrator.context import PromptEnvelope, prepare_prompt
from converge_orchestrator.models import (
    AgentConfig,
    ComplianceEntry,
    ComplianceSnapshot,
    ModelGatewayConfig,
    ModelProfile,
    ProjectConfig,
    Requirement,
    RequirementStatus,
    TaskEnvelope,
    TDDPlan,
)
from converge_orchestrator.targeting import targeted_plan

_TARGET_ID = "REQ-CONTEXT-001"
_SOURCE_ANCHOR = "CONVERGE_ORCHESTRATOR_2_0_SOURCE_OF_TRUTH.md:L269"
_STATEMENT = (
    "Planner context SHALL contain the exact target requirement and bounded "
    "current-repository evidence, while repository reconnaissance SHALL remain "
    "advisory rather than authority over the Source of Truth."
)
_SCOUT_BASE_COMMIT = "abc123def456"
_SCOUT_PATH = "src/converge_orchestrator/context.py"
_OTHER_ID = "ARCH-OTHER-001"


def _config(tmp_path: Path) -> ProjectConfig:
    requirements = tmp_path / "architecture.md"
    requirements.write_text(
        f"{_TARGET_ID} | {_SOURCE_ANCHOR} | {_STATEMENT}\n"
        f"{_OTHER_ID} | architecture.md:2 | Some other requirement.\n",
        encoding="utf-8",
    )
    return ProjectConfig(
        repo_path=tmp_path,
        requirements_path=requirements,
        state_dir=tmp_path / ".converge",
        worktree_dir=tmp_path / ".converge" / "worktrees",
        require_spec_read_only=False,
        model_gateway=ModelGatewayConfig(kind="openwebui"),
        model_profiles={
            "planner": ModelProfile(
                model="planner-model", context_tokens=32000, output_tokens=4096
            ),
        },
        agents={
            "planner": AgentConfig(agent="converge-planner", model_profile="planner"),
        },
    )


def _scout_snapshot() -> dict[str, Any]:
    return {
        "source": "agent",
        "base_branch": "main",
        "base_commit": _SCOUT_BASE_COMMIT,
        "summary": "Bounded repository snapshot for the planner.",
        "stacks": ["python"],
        "key_paths": [_SCOUT_PATH],
        "test_paths": ["tests/test_req_context_001_planner_target_context.py"],
        "architecture_notes": ["Context module carries the prompt envelope."],
        "risk_notes": [],
        "requirement_hints": {_TARGET_ID: [_SCOUT_PATH]},
        "uncertainties": [],
        "warnings": [],
    }


def _state(tmp_path: Path) -> dict[str, Any]:
    compliance = ComplianceSnapshot(
        entries={
            _TARGET_ID: ComplianceEntry(
                requirement_id=_TARGET_ID, status=RequirementStatus.FAIL
            ),
            _OTHER_ID: ComplianceEntry(
                requirement_id=_OTHER_ID, status=RequirementStatus.PASS
            ),
        }
    )
    return {
        "config_path": str(tmp_path / "converge.yaml"),
        "run_id": "run-context-001",
        "requirements_hash": "spec-sha",
        "requirements": [
            Requirement(
                id=_TARGET_ID,
                statement=_STATEMENT,
                source=_SOURCE_ANCHOR,
                severity="mandatory",
            ).model_dump(mode="json"),
            Requirement(
                id=_OTHER_ID,
                statement="Some other requirement.",
                source="architecture.md:2",
                severity="mandatory",
            ).model_dump(mode="json"),
        ],
        "compliance": compliance.model_dump(mode="json"),
        "baseline": {
            "repo_scout": _scout_snapshot(),
            "deterministic": {"stack": "python"},
        },
        "iteration": 0,
        "repair_attempts": 0,
        "replan_attempts": 0,
    }


def _store() -> SimpleNamespace:
    return SimpleNamespace(write_json=Mock(), append_event=Mock())


def _valid_task() -> TaskEnvelope:
    return TaskEnvelope(
        id=f"{_TARGET_ID}-0038",
        requirement_ids=[_TARGET_ID],
        title="Pin REQ-CONTEXT-001 planner target context",
        objective=(
            "Add regression tests proving the targeted planner receives only the exact "
            "target requirement and advisory repository context."
        ),
        constraints=["Do not modify production code or public API"],
        allowed_paths=["src/**", "tests/**"],
        acceptance=["New regression tests pass"],
        max_diff_lines=250,
        risk="low",
        risk_flags=[],
        change_kind="test_only",
        tdd=TDDPlan(
            mode="not_applicable",
            test_paths=[],
            test_gate=None,
            expected_failure_pattern=None,
            rationale="Deterministic regression pin; no observable behavior changes.",
        ),
    )


def _agent_result(*, ok: bool, output: str) -> SimpleNamespace:
    return SimpleNamespace(ok=ok, output=output, context={"budget_status": "bounded"})


def _run_targeted_plan(tmp_path: Path) -> tuple[PromptEnvelope, ProjectConfig]:
    cfg = _config(tmp_path)
    (tmp_path / "src").mkdir()
    (tmp_path / "tests").mkdir()
    state = _state(tmp_path)
    store = _store()
    task = _valid_task()
    captured: list[tuple[str, Any, Path]] = []

    def _capture_invoke(role: str, prompt: Any, cwd: Path) -> SimpleNamespace:
        captured.append((role, prompt, cwd))
        return _agent_result(ok=True, output=task.model_dump_json())

    with (
        patch("converge_orchestrator.targeting.load_config", return_value=cfg),
        patch("converge_orchestrator.targeting.wf._write_compliance"),
        patch("converge_orchestrator.targeting.wf._evidence", return_value=store),
        patch(
            "converge_orchestrator.targeting.OpenCodeAdapter.invoke",
            side_effect=_capture_invoke,
        ),
    ):
        result = cast(Any, targeted_plan(cast(Any, state)))

    assert result["status"] == "planned"
    assert result["task"]["requirement_ids"] == [_TARGET_ID]
    assert len(captured) == 1
    role, prompt, cwd = captured[0]
    assert role == "planner"
    assert cwd == cfg.repo_path
    assert isinstance(prompt, PromptEnvelope)
    return prompt, cfg


def test_planner_core_contains_exact_target_requirement_only(tmp_path: Path) -> None:
    """The planner core must carry the exact target ID/statement/source and nothing else."""
    prompt, _cfg = _run_targeted_plan(tmp_path)

    expected_excerpt = f"{_TARGET_ID} | {_SOURCE_ANCHOR} | {_STATEMENT}"
    assert expected_excerpt in prompt.core

    assert _OTHER_ID not in prompt.core
    assert _SCOUT_BASE_COMMIT not in prompt.core
    assert _SCOUT_PATH not in prompt.core
    assert "base_commit" not in prompt.core
    assert "repository scout" not in prompt.core.lower()


def test_advisory_sections_carry_bounded_snapshot_and_target_working_memory(
    tmp_path: Path,
) -> None:
    """Repo-scout reconnaissance and working memory are advisory, not authoritative."""
    prompt, _cfg = _run_targeted_plan(tmp_path)

    section_names = [section.name for section in prompt.advisory]
    assert "repository scout snapshot" in section_names
    assert "working memory" in section_names

    scout_section = next(
        section for section in prompt.advisory if section.name == "repository scout snapshot"
    )
    snapshot = json.loads(scout_section.text)
    assert snapshot["base_commit"] == _SCOUT_BASE_COMMIT
    assert _SCOUT_PATH in snapshot["key_paths"]
    assert snapshot["requirement_hints"].get(_TARGET_ID) == [_SCOUT_PATH]

    memory_section = next(
        section for section in prompt.advisory if section.name == "working memory"
    )
    memory = json.loads(memory_section.text)
    assert memory["source"] == "deterministic LangGraph state; advisory only"
    assert memory["immutable_requirements_remain_authoritative"] is True
    assert memory["unresolved_mandatory_requirement_ids"] == [_TARGET_ID]
    assert memory["recommended_requirement_ids"] == []


def test_rendered_prompt_frames_reconnaissance_as_advisory_only(
    tmp_path: Path,
) -> None:
    """The rendered provider input literally marks scout and working memory as ADVISORY."""
    prompt, cfg = _run_targeted_plan(tmp_path)

    rendered, report = prepare_prompt(cfg, "planner", prompt)
    assert rendered.startswith(prompt.core)
    assert (
        "REPOSITORY SCOUT SNAPSHOT (ADVISORY; verify against repository and immutable requirements)"
        in rendered
    )
    assert "WORKING MEMORY (ADVISORY;" in rendered
    assert report.budget_status == "bounded"
    assert report.advisory_included == [
        "repository scout snapshot",
        "working memory",
    ]
