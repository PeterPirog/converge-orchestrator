from __future__ import annotations

import os
import types
from pathlib import Path
from unittest.mock import patch

from converge_orchestrator.graph import quality
from converge_orchestrator.models import GateResult, ProjectConfig
from converge_orchestrator.sandbox import ExecutionSandbox


def _container_config(tmp_path: Path) -> ProjectConfig:
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / ".git").mkdir()
    requirements = tmp_path / "architecture.md"
    requirements.write_text("System must remain isolated.\n", encoding="utf-8")
    return ProjectConfig(
        repo_path=repo,
        requirements_path=requirements,
        require_spec_read_only=False,
        model_gateway={
            "kind": "openwebui",
            "api_key_env": "OPENWEBUI_API_KEY",
        },
        sandbox={
            "mode": "container",
            "image": "converge-runtime:test",
            "agent_network": "converge-ai",
            "quality_network": "none",
            "pass_env": ["PROJECT_TOKEN"],
        },
        agents={},
    )


def _env_names(argv: list[str]) -> set[str]:
    names: set[str] = set()
    for index, value in enumerate(argv[:-1]):
        if value == "--env":
            names.add(argv[index + 1])
    return names


def test_container_agent_uses_hardened_runtime_and_allowlisted_env(
    tmp_path: Path,
    monkeypatch,
) -> None:
    cfg = _container_config(tmp_path)
    monkeypatch.setenv("OPENWEBUI_API_KEY", "gateway-secret")
    monkeypatch.setenv("PROJECT_TOKEN", "project-secret")
    monkeypatch.setenv("HOST_ONLY_SECRET", "must-not-enter-container")
    completed = types.SimpleNamespace(returncode=0, stdout="ok")

    with patch("converge_orchestrator.sandbox.subprocess.run", return_value=completed) as run:
        result = ExecutionSandbox(cfg).run(
            ["opencode", "run"],
            cwd=cfg.repo_path,
            scope="agent",
            writable_cwd=False,
        )

    assert result.returncode == 0
    argv = run.call_args.args[0]
    assert "--pull=never" in argv
    assert "--cap-drop=ALL" in argv
    assert "no-new-privileges:true" in argv
    assert "--read-only" in argv
    assert argv[argv.index("--network") + 1] == "converge-ai"
    mounts = [argv[index + 1] for index, value in enumerate(argv[:-1]) if value == "--mount"]
    assert any(str(cfg.repo_path) in mount and mount.endswith(",readonly") for mount in mounts)
    passed = _env_names(argv)
    assert "OPENWEBUI_API_KEY" in passed
    assert "PROJECT_TOKEN" in passed
    assert "HOST_ONLY_SECRET" not in passed


def test_builder_worktree_is_writable_but_shared_git_metadata_is_read_only(
    tmp_path: Path,
) -> None:
    cfg = _container_config(tmp_path)
    worktree = tmp_path / "state" / "worktrees" / "arch-001"
    worktree.mkdir(parents=True)
    completed = types.SimpleNamespace(returncode=0, stdout="ok")

    with patch("converge_orchestrator.sandbox.subprocess.run", return_value=completed) as run:
        ExecutionSandbox(cfg).run(
            ["opencode", "run"],
            cwd=worktree,
            scope="agent",
            writable_cwd=True,
        )

    argv = run.call_args.args[0]
    mounts = [argv[index + 1] for index, value in enumerate(argv[:-1]) if value == "--mount"]
    worktree_mount = next(mount for mount in mounts if str(worktree) in mount)
    git_mount = next(mount for mount in mounts if str(cfg.repo_path / ".git") in mount)
    assert not worktree_mount.endswith(",readonly")
    assert git_mount.endswith(",readonly")


def test_quality_network_is_separate_from_agent_network(tmp_path: Path) -> None:
    cfg = _container_config(tmp_path)
    completed = types.SimpleNamespace(returncode=0, stdout="ok")

    with patch("converge_orchestrator.sandbox.subprocess.run", return_value=completed) as run:
        ExecutionSandbox(cfg).run(
            ["python", "-m", "pytest"],
            cwd=cfg.repo_path,
            scope="quality",
            writable_cwd=True,
        )

    argv = run.call_args.args[0]
    assert argv[argv.index("--network") + 1] == "none"


def test_active_graph_measures_scope_only_after_quality_commands(tmp_path: Path) -> None:
    cfg = ProjectConfig(
        repo_path=tmp_path,
        requirements_path=tmp_path / "architecture.md",
        require_spec_read_only=False,
        agents={},
    )
    state = {
        "config_path": str(tmp_path / "converge.yaml"),
        "run_id": "run-1",
        "task": {
            "id": "ARCH-001-1",
            "requirement_ids": ["ARCH-001"],
            "title": "bounded",
            "objective": "bounded",
            "allowed_paths": ["src/**"],
        },
        "worktree": str(tmp_path),
    }
    order: list[str] = []
    gate = GateResult(name="tests", ok=True, required=True, returncode=0, output="")
    scope = GateResult(name="diff_scope", ok=True, required=True, returncode=0, output="")
    store = types.SimpleNamespace(write_json=lambda *args, **kwargs: None)

    with (
        patch("converge_orchestrator.graph.load_config", return_value=cfg),
        patch(
            "converge_orchestrator.graph.run_quality_gates",
            side_effect=lambda *args: order.append("quality") or [gate],
        ),
        patch(
            "converge_orchestrator.graph.run_scope_gate",
            side_effect=lambda *args: order.append("scope") or scope,
        ),
        patch("converge_orchestrator.graph.wf._evidence", return_value=store),
    ):
        result = quality(state)

    assert order == ["quality", "scope"]
    assert [item["name"] for item in result["quality_results"]] == ["tests", "diff_scope"]
