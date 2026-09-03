from __future__ import annotations

import json
import subprocess
from pathlib import Path
from unittest.mock import patch

from converge_orchestrator.inspector import inspect_repository
from converge_orchestrator.models import ProjectConfig, QualityGate
from converge_orchestrator.quality import effective_quality_gates, run_quality_gates


def _config(tmp_path: Path, **kwargs) -> ProjectConfig:
    requirements = tmp_path / "architecture.md"
    requirements.write_text("System must remain testable.\n", encoding="utf-8")
    return ProjectConfig(
        repo_path=tmp_path,
        requirements_path=requirements,
        agents={},
        **kwargs,
    )


def test_inspector_discovers_python_quality_commands(tmp_path: Path) -> None:
    (tmp_path / "pyproject.toml").write_text(
        """
[project]
name = "fixture"
version = "0.1.0"

[project.optional-dependencies]
dev = ["pytest>=8", "ruff>=0.8", "mypy>=1"]

[tool.pytest.ini_options]
testpaths = ["tests"]

[tool.ruff]
line-length = 100

[tool.mypy]
python_version = "3.11"
""".strip()
        + "\n",
        encoding="utf-8",
    )

    profile = inspect_repository(tmp_path)
    commands = {gate.name: gate.command for gate in profile.quality_gates}
    assert profile.stacks == ["python"]
    assert commands["auto:python:test"] == ["python", "-m", "pytest", "-q"]
    assert commands["auto:python:lint"] == ["ruff", "check", "."]
    assert commands["auto:python:typecheck"] == ["mypy", "."]


def test_inspector_discovers_node_go_and_rust_without_graph_changes(tmp_path: Path) -> None:
    (tmp_path / "package.json").write_text(
        json.dumps(
            {
                "scripts": {
                    "test": "vitest run",
                    "lint": "eslint .",
                    "typecheck": "tsc --noEmit",
                }
            }
        ),
        encoding="utf-8",
    )
    (tmp_path / "pnpm-lock.yaml").write_text("lockfileVersion: 9\n", encoding="utf-8")
    (tmp_path / "go.mod").write_text("module example.invalid/fixture\n", encoding="utf-8")
    (tmp_path / "Cargo.toml").write_text(
        "[package]\nname='fixture'\nversion='0.1.0'\n",
        encoding="utf-8",
    )

    profile = inspect_repository(tmp_path)
    commands = {gate.name: gate.command for gate in profile.quality_gates}
    assert profile.stacks == ["node", "go", "rust"]
    assert profile.package_manager == "pnpm"
    assert commands["auto:node:test"] == ["pnpm", "test"]
    assert commands["auto:node:lint"] == ["pnpm", "lint"]
    assert commands["auto:go:test"] == ["go", "test", "./..."]
    assert commands["auto:rust:test"] == ["cargo", "test", "--all"]


def test_explicit_quality_command_suppresses_exact_discovery_duplicate(tmp_path: Path) -> None:
    (tmp_path / "pyproject.toml").write_text(
        "[project]\nname='fixture'\nversion='0.1.0'\n[tool.ruff]\nline-length=100\n",
        encoding="utf-8",
    )
    explicit = QualityGate(name="lint", command=["ruff", "check", "."])
    cfg = _config(tmp_path, quality_gates=[explicit])

    gates = effective_quality_gates(cfg, tmp_path)
    assert [gate.command for gate in gates].count(["ruff", "check", "."]) == 1
    assert gates[0].name == "lint"


def test_quality_adapter_normalizes_missing_tool_and_timeout(tmp_path: Path) -> None:
    missing = QualityGate(name="missing", command=["definitely-not-a-real-tool"])
    cfg = _config(tmp_path, quality_gates=[missing], auto_discover_quality=False)
    result = run_quality_gates(cfg, tmp_path)[0]
    assert not result.ok
    assert result.returncode == 127

    timeout_gate = QualityGate(name="timeout", command=["tool"], timeout_seconds=7)
    cfg = _config(tmp_path, quality_gates=[timeout_gate], auto_discover_quality=False)
    with patch(
        "converge_orchestrator.quality.ExecutionSandbox.run",
        side_effect=subprocess.TimeoutExpired(cmd=["tool"], timeout=7),
    ):
        result = run_quality_gates(cfg, tmp_path)[0]
    assert not result.ok
    assert result.returncode == 124
