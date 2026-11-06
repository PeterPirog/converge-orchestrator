"""Pin REQ-CONTEXT-005: bounded, compactable advisory context; durable audit source.

REQ-CONTEXT-005 (CONVERGE_ORCHESTRATOR_2_0_SOURCE_OF_TRUTH.md:L273, 8. Context and
role handoff) requires that advisory repository maps and working memory remain
bounded and compactable, while the information needed for resume, verification,
review, or audit lives in durable state or evidence rather than model-session
memory.

These contract tests exercise ``converge_orchestrator.context.build_working_memory``
and ``converge_orchestrator.context.prepare_prompt`` with synthetic, oversized
run-state. They prove:

* the working-memory projection stays bounded (unresolved ids ≤ 64, scout
  warnings ≤ 8) and explicitly declares truncation;
* the bounded projection is JSON-serializable and compactable;
* the projection is read-only with respect to the durable baseline, which keeps
  the complete scout warnings list and iteration/status continuity;
* advisory repository-map / working-memory material compacts under a tight
  context budget without touching the authoritative core prompt.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, cast

from converge_orchestrator.context import (
    AdvisorySection,
    PromptEnvelope,
    build_working_memory,
    prepare_prompt,
)
from converge_orchestrator.models import AgentConfig, ModelProfile, ProjectConfig, WorkflowState


def _project_config(tmp_path: Path, *, context_tokens: int = 6000) -> ProjectConfig:
    """Minimal deterministic config for prepare_prompt budget tests."""
    repo = tmp_path / "repo"
    repo.mkdir(exist_ok=True)
    requirements = tmp_path / "architecture.md"
    requirements.write_text("System must remain correct.\n", encoding="utf-8")
    return ProjectConfig(
        repo_path=repo,
        requirements_path=requirements,
        require_spec_read_only=False,
        context_input_fraction=0.5,
        context_output_reserve_tokens=1000,
        model_profiles={
            "planner": ModelProfile(
                model="openai/test-model",
                context_tokens=context_tokens,
            )
        },
        agents={
            "planner": AgentConfig(
                agent="converge-planner",
                model_profile="planner",
            )
        },
    )


def _oversized_state() -> dict[str, Any]:
    """Return a deterministic WorkflowState-shaped dict that exceeds every bound."""
    mandatory_ids = [f"REQ-M{index:03d}" for index in range(80)]
    recommended_ids = [f"REQ-R{index:03d}" for index in range(70)]
    requirements: list[dict[str, Any]] = [
        {"id": item, "severity": "mandatory", "statement": f"Statement {item}"}
        for item in mandatory_ids
    ] + [
        {"id": item, "severity": "recommended", "statement": f"Statement {item}"}
        for item in recommended_ids
    ]
    entries = {
        item: {"requirement_id": item, "status": "fail", "evidence": []}
        for item in mandatory_ids + recommended_ids
    }
    scout_warnings = [f"scout warning {index:04d}" for index in range(25)]
    return {
        "requirements": requirements,
        "compliance": {"entries": entries},
        "iteration": 42,
        "status": "replanning",
        "repair_attempts": 3,
        "replan_attempts": 2,
        "baseline": {
            "repo_scout": {
                "source": "agent",
                "base_commit": "abc1234",
                "warnings": scout_warnings,
            }
        },
        "review_result": {
            "findings": [
                {
                    "severity": "blocking",
                    "requirement_id": f"REQ-{index:03d}",
                    "file": "src/foo.py",
                    "reason": "x" * 600,
                }
                for index in range(14)
            ]
        },
        "task": {
            "id": "REQ-CONTEXT-005-0038",
            "title": "Bounded durable context",
            "requirement_ids": [f"REQ-{index:03d}" for index in range(35)],
            "objective": "y" * 1200,
        },
        "ci": {"status": "fail"},
    }


def test_working_memory_stays_bounded_with_truncation_flags() -> None:
    """Projection over oversized state keeps every surface within its declared bound."""
    state = _oversized_state()

    memory = build_working_memory(cast(WorkflowState, state))

    assert len(memory["unresolved_mandatory_requirement_ids"]) == 64
    assert memory["unresolved_ids_truncated"] is True
    assert len(memory["recommended_requirement_ids"]) == 64
    assert memory["recommended_ids_truncated"] is True
    assert len(memory["last_review_findings"]) == 12
    assert memory["last_review_findings_truncated"] is True
    assert memory["last_review_findings_total"] == 14
    assert memory["last_task"] is not None
    assert len(memory["last_task"]["requirement_ids"]) == 32
    assert memory["last_task"]["requirement_ids_truncated"] is True
    assert len(memory["last_task"]["objective"]) <= 1000
    assert memory["last_task"]["objective_truncated"] is True
    assert len(memory["repo_scout"]["warnings"]) == 8
    assert memory["iteration"] == 42
    assert memory["status"] == "replanning"


def test_working_memory_is_json_serializable_and_compactable() -> None:
    """The bounded projection round-trips through JSON and carries no large payloads."""
    state = _oversized_state()
    memory = build_working_memory(cast(WorkflowState, state))

    serialized = json.dumps(memory, ensure_ascii=False, sort_keys=True)
    deserialized = json.loads(serialized)

    assert deserialized == memory
    # Compactability: every advisory string is bounded (objective/reason) or capped
    # by an explicit list bound (ids, findings, warnings). No raw requirement
    # statements or full evidence bodies leak into the artifact.
    assert "Statement REQ-M" not in serialized
    assert "scout warning 0024" not in serialized
    assert len(serialized.encode("utf-8")) < 32_000


def test_working_memory_projection_never_mutates_durable_baseline() -> None:
    """The read-only projection leaves the durable baseline and compliance intact."""
    state = _oversized_state()
    original_baseline = json.dumps(state["baseline"], ensure_ascii=False, sort_keys=True)
    original_compliance = json.dumps(state["compliance"], ensure_ascii=False, sort_keys=True)
    original_requirements = json.dumps(state["requirements"], ensure_ascii=False, sort_keys=True)

    build_working_memory(cast(WorkflowState, state))

    assert json.dumps(state["baseline"], ensure_ascii=False, sort_keys=True) == original_baseline
    assert (
        json.dumps(state["compliance"], ensure_ascii=False, sort_keys=True)
        == original_compliance
    )
    assert (
        json.dumps(state["requirements"], ensure_ascii=False, sort_keys=True)
        == original_requirements
    )
    # The durable baseline still carries the full 25 scout warnings, not the 8-item
    # advisory projection.
    assert len(state["baseline"]["repo_scout"]["warnings"]) == 25
    assert "scout warning 0024" in state["baseline"]["repo_scout"]["warnings"]


def test_durable_state_carries_full_resume_and_audit_information() -> None:
    """Resume/audit facts (complete scout warnings, iteration/status continuity) live
    in durable state, not only in the bounded working-memory projection."""
    state = _oversized_state()
    memory = build_working_memory(cast(WorkflowState, state))

    durable = state["baseline"]["repo_scout"]
    assert durable["source"] == "agent"
    assert durable["base_commit"] == "abc1234"
    assert len(durable["warnings"]) == 25

    # Bounded projection is only advisory continuity; durable state holds the truth.
    assert memory["repo_scout"]["source"] == durable["source"]
    assert memory["repo_scout"]["base_commit"] == durable["base_commit"]
    assert len(memory["repo_scout"]["warnings"]) < len(durable["warnings"])

    # Iteration and status are present in both durable state and bounded memory.
    assert memory["iteration"] == state["iteration"]
    assert memory["status"] == state["status"]


def test_advisory_scout_and_working_memory_compact_under_tight_budget(tmp_path: Path) -> None:
    """prepare_prompt compacts advisory repository-map / working-memory sections while
    keeping the authoritative core intact."""
    cfg = _project_config(tmp_path, context_tokens=6000)
    core = "CORE:" + ("a" * 2500)
    oversized_snapshot = {
        "source": "agent",
        "base_commit": "abc1234",
        "warnings": [f"warning {index:04d}" for index in range(25)],
    }
    oversized_memory = build_working_memory(cast(WorkflowState, _oversized_state()))
    envelope = PromptEnvelope(
        core=core,
        advisory=(
            AdvisorySection(
                "repository scout snapshot",
                json.dumps(oversized_snapshot, ensure_ascii=False, indent=2),
            ),
            AdvisorySection(
                "working memory",
                json.dumps(oversized_memory, ensure_ascii=False, indent=2),
            ),
        ),
    )

    rendered, report = prepare_prompt(cfg, "planner", envelope)

    assert rendered.startswith(core)
    assert report.budget_status == "bounded"
    assert report.input_budget_tokens is not None
    assert report.estimated_input_tokens <= report.input_budget_tokens
    # At least one advisory section is truncated or dropped, proving compactability.
    assert report.advisory_truncated or report.advisory_dropped
    assert len(report.advisory_included) <= len(envelope.advisory)
    # Core prompt never shrinks or is rewritten.
    assert core in rendered
    assert rendered.index(core) == 0
