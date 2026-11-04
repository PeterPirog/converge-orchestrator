"""Pin REQ-AGENT-015: one validated Task Envelope per deterministic target.

REQ-AGENT-015 (CONVERGE_ORCHESTRATOR_2_0_SOURCE_OF_TRUTH.md:L248
[7. Deterministic architecture and agent roles]): Planning SHALL produce one validated Task
Envelope for the deterministic target requirement.

These deterministic assertions lock ``targeted_plan`` planner-contract validation:

* Any Task Envelope whose ``requirement_ids`` is not exactly ``[target.id]`` is rejected as a
  bounded contract failure.
* A schema-valid envelope naming only the deterministic target is accepted and advances the
  workflow with ``status=planned``.
* The accepted Task Envelope is persisted as ``<run_id>/<task_id>/task.json`` before the
  candidate proceeds.

No network, model provider, git command, subprocess call, or filesystem mutation outside the test
``tmp_path`` is performed.
"""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import patch

import pytest

from converge_orchestrator.models import (
    AgentConfig,
    ComplianceEntry,
    ComplianceSnapshot,
    ProjectConfig,
    Requirement,
    RequirementStatus,
    TaskEnvelope,
)
from converge_orchestrator.targeting import route_after_targeted_plan, targeted_plan

_TARGET_ID = "REQ-AGENT-015"
_SOURCE_ANCHOR = "CONVERGE_ORCHESTRATOR_2_0_SOURCE_OF_TRUTH.md:248"


def _requirement() -> Requirement:
    return Requirement(
        id=_TARGET_ID,
        statement="Planning SHALL produce one validated Task Envelope per deterministic target.",
        source=_SOURCE_ANCHOR,
    )


def _config(tmp_path: Path) -> ProjectConfig:
    repo = tmp_path / "repo"
    repo.mkdir(exist_ok=True)
    requirements = tmp_path / "architecture.md"
    requirements.write_text("System must remain deterministic.\n", encoding="utf-8")
    return ProjectConfig(
        repo_path=repo,
        requirements_path=requirements,
        state_dir=tmp_path / "state",
        worktree_dir=tmp_path / "state" / "worktrees",
        agents={"planner": AgentConfig(agent="converge-planner", model="openai/planner")},
        require_spec_read_only=False,
    )


def _compliance(status: RequirementStatus = RequirementStatus.FAIL) -> ComplianceSnapshot:
    return ComplianceSnapshot(
        entries={
            _TARGET_ID: ComplianceEntry(
                requirement_id=_TARGET_ID,
                status=status,
            )
        }
    )


def _evidence_dir(cfg: ProjectConfig) -> Path:
    assert cfg.state_dir is not None
    return cast(Path, cfg.state_dir / "evidence")


def _state(tmp_path: Path) -> dict:
    return {
        "config_path": str(tmp_path / "converge.yaml"),
        "run_id": "run-1",
        "requirements_hash": "spec-sha",
        "requirements": [_requirement().model_dump(mode="json")],
        "compliance": _compliance().model_dump(mode="json"),
        "baseline": {"repo_scout": {"base_commit": "base-sha"}},
        "iteration": 0,
        "replan_attempts": 0,
    }


def _agent_result(*, ok: bool, output: str) -> SimpleNamespace:
    return SimpleNamespace(
        ok=ok,
        output=output,
        context={"budget_status": "bounded"},
    )


def _valid_task() -> TaskEnvelope:
    return TaskEnvelope(
        id="REQ-AGENT-015-1",
        requirement_ids=[_TARGET_ID],
        title="Bounded deterministic target",
        objective="Produce one validated Task Envelope for the deterministic target requirement.",
    )


@pytest.mark.parametrize(
    "wrong_ids",
    [
        ["OTHER-001"],
        [_TARGET_ID, "OTHER-001"],
        [],
    ],
    ids=["wrong_single_id", "extra_id", "empty_ids"],
)
def test_targeted_plan_rejects_envelope_with_wrong_requirement_ids(
    tmp_path: Path, wrong_ids: list[str]
) -> None:
    """A Task Envelope must name exactly the deterministic target; any other list
    is a contract failure."""
    cfg = _config(tmp_path)
    state = _state(tmp_path)
    wrong = _valid_task().model_copy(update={"requirement_ids": wrong_ids})

    with (
        patch("converge_orchestrator.targeting.load_config", return_value=cfg),
        patch("converge_orchestrator.workflow.load_config", return_value=cfg),
        patch("converge_orchestrator.targeting.wf._write_compliance"),
        patch(
            "converge_orchestrator.targeting.OpenCodeAdapter.invoke",
            return_value=_agent_result(ok=True, output=wrong.model_dump_json()),
        ),
    ):
        result: dict[str, Any] = targeted_plan(state)  # type: ignore[arg-type]

    assert result["status"] == "planner_retry"
    assert result["task"] is None
    assert route_after_targeted_plan(cast(Any, result)) == "retry"

    control = result["baseline"]["planner_control"]
    assert control["target_requirement_id"] == _TARGET_ID
    assert control["attempts"] == 1
    assert control["last_failure_kind"] == "contract"
    assert _TARGET_ID in result["message"]

    # The rejected envelope must not be persisted as a candidate task.
    assert not (_evidence_dir(cfg) / "run-1" / wrong.id / "task.json").exists()


def test_targeted_plan_accepts_single_validated_envelope_and_persists_task_json(
    tmp_path: Path,
) -> None:
    """A schema-valid envelope naming only the target is accepted, and
    task.json is persisted first."""
    cfg = _config(tmp_path)
    state = _state(tmp_path)
    task = _valid_task()

    with (
        patch("converge_orchestrator.targeting.load_config", return_value=cfg),
        patch("converge_orchestrator.workflow.load_config", return_value=cfg),
        patch("converge_orchestrator.targeting.wf._write_compliance"),
        patch(
            "converge_orchestrator.targeting.OpenCodeAdapter.invoke",
            return_value=_agent_result(ok=True, output=task.model_dump_json()),
        ),
    ):
        result: dict[str, Any] = targeted_plan(state)  # type: ignore[arg-type]

    assert result["status"] == "planned"
    assert route_after_targeted_plan(cast(Any, result)) == "prepare"
    assert result["task"]["id"] == task.id
    assert result["task"]["requirement_ids"] == [_TARGET_ID]

    control = result["baseline"]["planner_control"]
    assert control["target_requirement_id"] == _TARGET_ID
    assert control["attempts"] == 0
    assert control["last_failure_kind"] is None

    artifact = _evidence_dir(cfg) / "run-1" / task.id / "task.json"
    assert artifact.exists(), "task.json evidence must be persisted before the candidate proceeds"
    persisted = json.loads(artifact.read_text(encoding="utf-8"))
    assert persisted["id"] == task.id
    assert persisted["requirement_ids"] == [_TARGET_ID]
    assert persisted == result["task"]

    events_file = _evidence_dir(cfg) / "run-1" / "events.jsonl"
    assert events_file.exists()
    events = [
        json.loads(line)["event"]
        for line in events_file.read_text(encoding="utf-8").splitlines()
    ]
    assert "planned" in events
