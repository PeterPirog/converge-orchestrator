from __future__ import annotations

import json
import sys
from pathlib import Path
from unittest.mock import patch

from converge_orchestrator.models import ProjectConfig, QualityGate, TaskEnvelope
from converge_orchestrator.quality import run_scope_gate
from converge_orchestrator.spec import compile_contract
from converge_orchestrator.verification import run_requirement_verifiers


def _config(tmp_path: Path, baseline: Path) -> ProjectConfig:
    requirements = tmp_path / "architecture.md"
    requirements.write_text(
        "# Rules\n\nARCH-001 must keep health marker.\nARCH-002 must keep API stable.\n",
        encoding="utf-8",
    )
    command = [
        sys.executable,
        "-c",
        "from pathlib import Path; raise SystemExit(0 if Path('health.txt').read_text().strip() == 'ok' else 1)",
    ]
    return ProjectConfig(
        repo_path=baseline,
        requirements_path=requirements,
        agents={},
        requirement_verifiers={
            "ARCH-001": [QualityGate(name="health-marker", command=command)]
        },
    )


def test_requirement_verifier_reports_deterministic_pass_and_fail(tmp_path: Path) -> None:
    baseline = tmp_path / "baseline"
    candidate = tmp_path / "candidate"
    baseline.mkdir()
    candidate.mkdir()
    (baseline / "health.txt").write_text("ok\n", encoding="utf-8")
    (candidate / "health.txt").write_text("broken\n", encoding="utf-8")
    cfg = _config(tmp_path, baseline)
    requirements = compile_contract(cfg.requirements_path).requirements

    baseline_results = run_requirement_verifiers(cfg, baseline, requirements)
    candidate_results = run_requirement_verifiers(cfg, candidate, requirements)
    baseline_by_id = {item.requirement_id: item for item in baseline_results}
    candidate_by_id = {item.requirement_id: item for item in candidate_results}

    assert baseline_by_id["ARCH-001"].status.value == "pass"
    assert candidate_by_id["ARCH-001"].status.value == "fail"
    assert candidate_by_id["ARCH-002"].status.value == "unverified"
    assert candidate_by_id["ARCH-001"].gates[0].returncode == 1


def test_scope_gate_blocks_new_mandatory_requirement_regression(tmp_path: Path) -> None:
    baseline = tmp_path / "baseline"
    candidate = tmp_path / "candidate"
    baseline.mkdir()
    candidate.mkdir()
    (baseline / "health.txt").write_text("ok\n", encoding="utf-8")
    (candidate / "health.txt").write_text("broken\n", encoding="utf-8")
    cfg = _config(tmp_path, baseline)
    task = TaskEnvelope(
        id="ARCH-002-1",
        requirement_ids=["ARCH-002"],
        title="unrelated task",
        objective="preserve existing architecture",
        allowed_paths=["**"],
    )

    with (
        patch("converge_orchestrator.quality.changed_files", return_value=["health.txt"]),
        patch("converge_orchestrator.quality.diff_line_count", return_value=2),
    ):
        result = run_scope_gate(cfg, candidate, task)

    details = json.loads(result.output)
    assert not result.ok
    assert details["convergence"]["mandatory_regressions"] == 1


def test_scope_gate_accepts_configured_target_only_after_deterministic_progress(
    tmp_path: Path,
) -> None:
    baseline = tmp_path / "baseline"
    candidate = tmp_path / "candidate"
    baseline.mkdir()
    candidate.mkdir()
    (baseline / "health.txt").write_text("broken\n", encoding="utf-8")
    (candidate / "health.txt").write_text("ok\n", encoding="utf-8")
    cfg = _config(tmp_path, baseline)
    task = TaskEnvelope(
        id="ARCH-001-1",
        requirement_ids=["ARCH-001"],
        title="repair health rule",
        objective="make ARCH-001 pass",
        allowed_paths=["**"],
    )

    with (
        patch("converge_orchestrator.quality.changed_files", return_value=["health.txt"]),
        patch("converge_orchestrator.quality.diff_line_count", return_value=2),
    ):
        result = run_scope_gate(cfg, candidate, task)

    details = json.loads(result.output)
    assert result.ok
    assert details["convergence"]["target_improved"] == ["ARCH-001"]
