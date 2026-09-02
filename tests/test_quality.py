from pathlib import Path
from unittest.mock import patch

from converge_orchestrator.models import GateResult, ProjectConfig, TaskEnvelope
from converge_orchestrator.quality import required_gates_pass, run_scope_gate


def test_optional_failure_does_not_block() -> None:
    results = [
        GateResult(name="tests", ok=True, required=True, returncode=0, output=""),
        GateResult(name="coverage", ok=False, required=False, returncode=1, output=""),
    ]
    assert required_gates_pass(results)


def test_required_failure_blocks() -> None:
    results = [
        GateResult(name="tests", ok=False, required=True, returncode=1, output="failed")
    ]
    assert not required_gates_pass(results)


def test_scope_gate_blocks_out_of_scope_change(tmp_path: Path) -> None:
    cfg = ProjectConfig(
        repo_path=tmp_path,
        requirements_path=tmp_path / "architecture.md",
        agents={},
        max_diff_lines_hard=1000,
    )
    task = TaskEnvelope(
        id="ARCH-1",
        requirement_ids=["ARCH-1"],
        title="task",
        objective="bounded change",
        allowed_paths=["src/**", "tests/**"],
        max_diff_lines=100,
    )
    with (
        patch("converge_orchestrator.quality.changed_files", return_value=["docs/notes.md"]),
        patch("converge_orchestrator.quality.diff_line_count", return_value=10),
    ):
        result = run_scope_gate(cfg, tmp_path, task)
    assert not result.ok
    assert result.required
