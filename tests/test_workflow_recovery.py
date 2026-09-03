from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from converge_orchestrator.models import ComplianceSnapshot, GateResult, ProjectConfig, TaskEnvelope
from converge_orchestrator.workflow import integrate


def _config(tmp_path: Path) -> ProjectConfig:
    requirements = tmp_path / "architecture.md"
    requirements.write_text("immutable\n", encoding="utf-8")
    return ProjectConfig(
        repo_path=tmp_path,
        requirements_path=requirements,
        require_spec_read_only=False,
        agents={},
    )


def _state(tmp_path: Path) -> dict:
    task = TaskEnvelope(
        id="ARCH-001-1",
        requirement_ids=["ARCH-001"],
        title="Recover integration",
        objective="Preserve the candidate across a process crash",
        allowed_paths=["src/**"],
    )
    gate = GateResult(
        name="tests",
        ok=True,
        required=True,
        returncode=0,
        output="pass",
    )
    return {
        "config_path": str(tmp_path / "converge.yaml"),
        "run_id": "run-1",
        "requirements_hash": "spec-hash",
        "task": task.model_dump(mode="json"),
        "worktree": str(tmp_path / "worktree"),
        "branch": "converge/arch-001-1",
        "quality_results": [gate.model_dump(mode="json")],
        "compliance": ComplianceSnapshot().model_dump(mode="json"),
    }


def test_integrate_recovers_commit_created_before_langgraph_checkpoint(
    tmp_path: Path,
) -> None:
    cfg = _config(tmp_path)
    state = _state(tmp_path)
    store = SimpleNamespace(append_event=Mock())

    with (
        patch("converge_orchestrator.workflow.load_config", return_value=cfg),
        patch("converge_orchestrator.workflow.sha256_file", return_value="spec-hash"),
        patch("converge_orchestrator.workflow.commit_all", return_value=None),
        patch(
            "converge_orchestrator.workflow.existing_candidate_commit",
            return_value="candidate-sha",
        ) as recovered,
        patch("converge_orchestrator.workflow.push") as push,
        patch("converge_orchestrator.workflow._evidence", return_value=store),
        patch("converge_orchestrator.workflow._write_compliance"),
    ):
        result = integrate(state)

    recovered.assert_called_once_with(Path(state["worktree"]), "main")
    push.assert_called_once_with(Path(state["worktree"]), state["branch"])
    assert result["commit_sha"] == "candidate-sha"
    assert result["status"] == "pushed"
    event = store.append_event.call_args.args[2]
    assert event["recovered_existing_commit"] is True
