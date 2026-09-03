from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

from converge_orchestrator.models import ProjectConfig, Requirement, TaskEnvelope
from converge_orchestrator.prompts import builder_prompt, repair_prompt
from converge_orchestrator.workflow import build_graph, integrate


def _task() -> TaskEnvelope:
    return TaskEnvelope(
        id="ARCH-002-0001",
        requirement_ids=["ARCH-002"],
        title="Enforce boundary",
        objective="Remove a forbidden dependency",
        allowed_paths=["src/**", "tests/**"],
        acceptance=["Architecture test passes"],
    )


def _requirements() -> list[Requirement]:
    return [
        Requirement(
            id="ARCH-001",
            statement="Unrelated requirement that Builder should not receive.",
            source="architecture.md:L10-L10",
        ),
        Requirement(
            id="ARCH-002",
            statement="Domain must not depend on infrastructure.",
            source="architecture.md:L20-L20",
        ),
    ]


def test_builder_and_repair_receive_only_target_requirement_statements() -> None:
    task = _task()
    requirements = _requirements()

    build_text = builder_prompt(task, requirements)
    repair_text = repair_prompt(task, requirements, [], None)

    for text in (build_text, repair_text):
        assert "ARCH-002 | architecture.md:L20-L20" in text
        assert "Domain must not depend on infrastructure." in text
        assert "Unrelated requirement that Builder should not receive." not in text
        assert "Node" in text
        assert "entry point" in text


def test_integrator_rechecks_spec_hash_immediately_before_commit(tmp_path: Path) -> None:
    requirements = tmp_path / "architecture.md"
    requirements.write_text("Immutable intent\n", encoding="utf-8")
    repo = tmp_path / "repository"
    repo.mkdir()
    cfg = ProjectConfig(
        repo_path=repo,
        requirements_path=requirements,
        agents={},
        require_spec_read_only=False,
    )
    state = {
        "config_path": str(tmp_path / "converge.yaml"),
        "requirements_hash": "expected-hash",
    }

    with (
        patch("converge_orchestrator.workflow.load_config", return_value=cfg),
        patch("converge_orchestrator.workflow.sha256_file", return_value="changed-hash"),
        patch("converge_orchestrator.workflow.commit_all") as commit_all,
    ):
        result = integrate(state)

    assert result["status"] == "spec_changed"
    commit_all.assert_not_called()


def test_graph_uses_separate_spec_guards_after_bootstrap_and_writes() -> None:
    graph = build_graph().get_graph()
    edges = {(edge.source, edge.target) for edge in graph.edges}

    assert ("bootstrap", "guard_plan") in edges
    assert ("build", "guard_quality") in edges
    assert ("repair", "guard_quality") in edges
    assert ("guard_plan", "quality") not in edges
    assert ("bootstrap", "quality") not in edges
