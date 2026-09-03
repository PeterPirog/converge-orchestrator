from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch

from converge_orchestrator.context import PromptEnvelope
from converge_orchestrator.graph import build_graph, plan, scout
from converge_orchestrator.models import AgentResult, ProjectConfig, Requirement
from converge_orchestrator.opencode_config import build_opencode_config


def _config(tmp_path: Path, *, include_scout: bool = True) -> ProjectConfig:
    repo = tmp_path / "repo"
    repo.mkdir(exist_ok=True)
    requirements = tmp_path / "architecture.md"
    requirements.write_text("System must remain modular.\n", encoding="utf-8")
    agents = {
        "planner": {"agent": "converge-planner", "model": "openai/planner"},
    }
    if include_scout:
        agents["scout"] = {"agent": "converge-scout", "model": "openai/scout"}
    return ProjectConfig(
        repo_path=repo,
        requirements_path=requirements,
        state_dir=tmp_path / "state",
        require_spec_read_only=False,
        agents=agents,
    )


def _state(tmp_path: Path) -> dict:
    requirement = Requirement(
        id="ARCH-001",
        statement="Domain logic must remain independent from infrastructure.",
        source="architecture.md:1",
    )
    return {
        "config_path": str(tmp_path / "converge.yaml"),
        "run_id": "run-1",
        "thread_id": "thread-1",
        "requirements_hash": "sha",
        "requirements": [requirement.model_dump(mode="json")],
        "baseline": {"deterministic": {"stack": "python"}},
        "compliance": {"entries": {}, "mandatory_regressions": 0},
        "iteration": 0,
        "repair_attempts": 0,
        "replan_attempts": 0,
        "risk_flags": [],
        "approved_risk_flags": [],
        "status": "spec_ok",
    }


def test_scout_role_is_read_only() -> None:
    cfg = ProjectConfig(
        repo_path=Path("/tmp/converge-scout-repo"),
        requirements_path=Path("/tmp/converge-scout-architecture.md"),
        require_spec_read_only=False,
        agents={"scout": {"agent": "converge-scout", "model": "openai/scout"}},
    )

    payload = build_opencode_config(cfg)
    permission = payload["agent"]["converge-scout"]["permission"]

    assert permission["edit"] == "deny"
    assert permission["bash"]["*"] == "deny"
    assert permission["task"] == "deny"
    assert permission["external_directory"] == "deny"


def test_scout_persists_bounded_snapshot_and_filters_unknown_requirement_hints(
    tmp_path: Path,
) -> None:
    cfg = _config(tmp_path)
    state = _state(tmp_path)
    output = {
        "summary": "Python service with a clear domain boundary.",
        "stacks": ["python", "python"],
        "key_paths": ["src/domain/service.py"],
        "test_paths": ["tests/test_service.py"],
        "architecture_notes": ["Domain currently imports infrastructure adapter."],
        "risk_notes": ["Public API is exposed by src/api.py."],
        "requirement_hints": {
            "ARCH-001": ["src/domain/service.py"],
            "ARCH-999": ["src/imaginary.py"],
        },
        "uncertainties": ["No integration fixture found."],
    }

    with (
        patch("converge_orchestrator.graph.load_config", return_value=cfg),
        patch("converge_orchestrator.workflow.load_config", return_value=cfg),
        patch("converge_orchestrator.graph.update_base", return_value="abc123"),
        patch(
            "converge_orchestrator.graph.OpenCodeAdapter.invoke",
            return_value=AgentResult(
                role="scout",
                ok=True,
                output=json.dumps(output),
                returncode=0,
            ),
        ),
    ):
        result = scout(state)

    snapshot = result["baseline"]["repo_scout"]
    assert result["baseline"]["deterministic"] == {"stack": "python"}
    assert snapshot["source"] == "agent"
    assert snapshot["base_commit"] == "abc123"
    assert snapshot["stacks"] == ["python"]
    assert snapshot["requirement_hints"] == {"ARCH-001": ["src/domain/service.py"]}
    assert any("ARCH-999" in warning for warning in snapshot["warnings"])
    evidence = cfg.state_dir / "evidence" / "run-1" / "run" / "repo-scout.json"
    assert json.loads(evidence.read_text(encoding="utf-8"))["base_commit"] == "abc123"


def test_scout_failure_falls_back_without_stopping_planning(tmp_path: Path) -> None:
    cfg = _config(tmp_path)
    state = _state(tmp_path)

    with (
        patch("converge_orchestrator.graph.load_config", return_value=cfg),
        patch("converge_orchestrator.workflow.load_config", return_value=cfg),
        patch("converge_orchestrator.graph.update_base", return_value="def456"),
        patch(
            "converge_orchestrator.graph.OpenCodeAdapter.invoke",
            return_value=AgentResult(
                role="scout",
                ok=False,
                output="model gateway unavailable",
                returncode=2,
            ),
        ),
    ):
        result = scout(state)

    snapshot = result["baseline"]["repo_scout"]
    assert result["status"] == "scout_fallback"
    assert snapshot["source"] == "fallback"
    assert snapshot["base_commit"] == "def456"
    assert "Planner must inspect" in snapshot["summary"]
    assert "model gateway unavailable" in snapshot["warnings"][0]


def test_planner_receives_the_scout_snapshot_as_advisory_context(tmp_path: Path) -> None:
    cfg = _config(tmp_path)
    state = _state(tmp_path)
    state["baseline"]["repo_scout"] = {
        "base_branch": "main",
        "base_commit": "abc123",
        "source": "agent",
        "summary": "Mapped service repository.",
        "stacks": ["python"],
        "key_paths": ["src/domain/service.py"],
        "test_paths": ["tests/test_service.py"],
        "architecture_notes": [],
        "risk_notes": [],
        "requirement_hints": {"ARCH-001": ["src/domain/service.py"]},
        "uncertainties": [],
        "warnings": [],
    }
    task = {
        "id": "ARCH-001-0001",
        "requirement_ids": ["ARCH-001"],
        "title": "Restore domain boundary",
        "objective": "Remove the infrastructure import from the domain service.",
        "constraints": [],
        "allowed_paths": ["src/**", "tests/**"],
        "acceptance": ["Relevant tests pass"],
        "max_diff_lines": 200,
        "risk": "low",
        "risk_flags": [],
    }

    with (
        patch("converge_orchestrator.graph.load_config", return_value=cfg),
        patch("converge_orchestrator.workflow.load_config", return_value=cfg),
        patch(
            "converge_orchestrator.graph.OpenCodeAdapter.invoke",
            return_value=AgentResult(
                role="planner",
                ok=True,
                output=json.dumps(task),
                returncode=0,
            ),
        ) as invoke,
    ):
        result = plan(state)

    prompt = invoke.call_args.args[1]
    assert isinstance(prompt, PromptEnvelope)
    assert "ARCH-001" in prompt.core
    assert "Domain logic must remain independent from infrastructure." in prompt.core
    scout_context = next(
        section for section in prompt.advisory if section.name == "repository scout snapshot"
    )
    assert '"base_commit": "abc123"' in scout_context.text
    assert "src/domain/service.py" in scout_context.text
    assert any(section.name == "working memory" for section in prompt.advisory)
    assert result["task"]["id"] == "ARCH-001-0001"
    assert result["iteration"] == 1


def test_composed_langgraph_places_scout_immediately_before_planner() -> None:
    graph = build_graph().get_graph()
    edges = {(edge.source, edge.target) for edge in graph.edges}

    assert ("pause_plan", "scout") in edges
    assert ("scout", "plan") in edges
    assert ("pause_plan", "plan") not in edges
