from __future__ import annotations

import json
import types
from pathlib import Path
from unittest.mock import patch

import pytest
from pydantic import ValidationError

from converge_orchestrator.graph import route_after_build_pause
from converge_orchestrator.models import GateResult, ProjectConfig, QualityGate, TaskEnvelope
from converge_orchestrator.tdd import run_tdd_baseline, run_tdd_green, run_tdd_red


def _config(tmp_path: Path) -> ProjectConfig:
    requirements = tmp_path / "architecture.md"
    requirements.write_text("System must expose the requested behavior.\n", encoding="utf-8")
    return ProjectConfig(
        repo_path=tmp_path,
        requirements_path=requirements,
        require_spec_read_only=False,
        agents={},
        auto_discover_quality=False,
        quality_gates=[
            QualityGate(
                name="unit-test",
                command=["python", "-m", "pytest", "-q"],
                timeout_seconds=30,
            )
        ],
    )


def _behavior_task() -> TaskEnvelope:
    return TaskEnvelope(
        id="ARCH-001-1",
        requirement_ids=["ARCH-001"],
        title="Add behavior",
        objective="Expose the required behavior",
        allowed_paths=["src/**", "tests/**"],
        change_kind="behavior",
        tdd={
            "mode": "required",
            "test_paths": ["tests/**"],
            "test_gate": "unit-test",
            "expected_failure_pattern": "NEW_RULE_MISSING",
            "rationale": "Observable behavior changes require a failing test first.",
        },
    )


def _completed(returncode: int, stdout: str):
    return types.SimpleNamespace(returncode=returncode, stdout=stdout)


def test_behavior_task_requires_structured_tdd_contract() -> None:
    with pytest.raises(ValidationError, match="behavior-changing tasks require"):
        TaskEnvelope(
            id="ARCH-001-1",
            requirement_ids=["ARCH-001"],
            title="Behavior",
            objective="Change behavior",
            change_kind="behavior",
        )


def test_non_behavior_task_can_explicitly_skip_tdd() -> None:
    task = TaskEnvelope(
        id="ARCH-001-2",
        requirement_ids=["ARCH-001"],
        title="Refactor",
        objective="Preserve behavior",
        change_kind="refactor",
        tdd={"mode": "not_applicable", "rationale": "No observable behavior change."},
    )

    state = {"task": task.model_dump(mode="json"), "status": "resumed_at_before_build"}
    assert route_after_build_pause(state) == "build"


def test_behavior_task_routes_through_red_phase() -> None:
    task = _behavior_task()
    state = {"task": task.model_dump(mode="json"), "status": "resumed_at_before_build"}
    assert route_after_build_pause(state) == "tdd_red"


def test_red_accepts_only_new_expected_failure_from_test_only_diff(tmp_path: Path) -> None:
    cfg = _config(tmp_path)
    task = _behavior_task()
    test_file = tmp_path / "tests" / "test_rule.py"
    test_file.parent.mkdir()

    with (
        patch(
            "converge_orchestrator.tdd.ExecutionSandbox.run",
            return_value=_completed(1, "existing unrelated failure"),
        ),
        patch("converge_orchestrator.tdd.changed_files", return_value=[]),
    ):
        baseline = run_tdd_baseline(cfg, tmp_path, task)

    assert baseline.ok
    baseline_payload = json.loads(baseline.output)
    assert baseline_payload["gate_returncode"] == 1

    test_file.write_text(
        "def test_new_rule():\n    assert False, 'NEW_RULE_MISSING'\n",
        encoding="utf-8",
    )
    with (
        patch(
            "converge_orchestrator.tdd.ExecutionSandbox.run",
            return_value=_completed(1, "AssertionError: NEW_RULE_MISSING"),
        ),
        patch(
            "converge_orchestrator.tdd.changed_files",
            return_value=["tests/test_rule.py"],
        ),
    ):
        red = run_tdd_red(cfg, tmp_path, task, baseline)

    assert red.ok
    payload = json.loads(red.output)
    assert payload["expected_signal_in_red"] is True
    assert payload["expected_signal_in_baseline"] is False
    assert set(payload["red_test_sha256"]) == {"tests/test_rule.py"}


def test_red_rejects_failure_signal_that_already_existed_at_baseline(tmp_path: Path) -> None:
    cfg = _config(tmp_path)
    task = _behavior_task()
    test_file = tmp_path / "tests" / "test_rule.py"
    test_file.parent.mkdir()

    with (
        patch(
            "converge_orchestrator.tdd.ExecutionSandbox.run",
            return_value=_completed(1, "old failure NEW_RULE_MISSING"),
        ),
        patch("converge_orchestrator.tdd.changed_files", return_value=[]),
    ):
        baseline = run_tdd_baseline(cfg, tmp_path, task)

    test_file.write_text("def test_new_rule():\n    assert False\n", encoding="utf-8")
    with (
        patch(
            "converge_orchestrator.tdd.ExecutionSandbox.run",
            return_value=_completed(1, "still NEW_RULE_MISSING"),
        ),
        patch(
            "converge_orchestrator.tdd.changed_files",
            return_value=["tests/test_rule.py"],
        ),
    ):
        red = run_tdd_red(cfg, tmp_path, task, baseline)

    assert not red.ok
    assert json.loads(red.output)["expected_signal_in_baseline"] is True


def test_red_rejects_production_changes_in_test_only_phase(tmp_path: Path) -> None:
    cfg = _config(tmp_path)
    task = _behavior_task()
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "service.py").write_text("enabled = True\n", encoding="utf-8")
    baseline = GateResult(
        name="tdd_baseline",
        ok=True,
        required=True,
        returncode=0,
        output=json.dumps({"gate_output": "baseline pass"}),
    )

    with (
        patch(
            "converge_orchestrator.tdd.ExecutionSandbox.run",
            return_value=_completed(1, "NEW_RULE_MISSING"),
        ),
        patch(
            "converge_orchestrator.tdd.changed_files",
            return_value=["src/service.py"],
        ),
    ):
        red = run_tdd_red(cfg, tmp_path, task, baseline)

    assert not red.ok
    assert json.loads(red.output)["test_paths_ok"] is False


def test_green_requires_exact_frozen_red_test_and_same_gate_to_pass(tmp_path: Path) -> None:
    cfg = _config(tmp_path)
    task = _behavior_task()
    test_file = tmp_path / "tests" / "test_rule.py"
    test_file.parent.mkdir()
    test_file.write_text(
        "def test_new_rule():\n    assert False, 'NEW_RULE_MISSING'\n",
        encoding="utf-8",
    )
    baseline = GateResult(
        name="tdd_baseline",
        ok=True,
        required=True,
        returncode=0,
        output=json.dumps({"gate_output": "baseline pass"}),
    )

    with (
        patch(
            "converge_orchestrator.tdd.ExecutionSandbox.run",
            return_value=_completed(1, "NEW_RULE_MISSING"),
        ),
        patch(
            "converge_orchestrator.tdd.changed_files",
            return_value=["tests/test_rule.py"],
        ),
    ):
        red = run_tdd_red(cfg, tmp_path, task, baseline)
    assert red.ok

    with patch(
        "converge_orchestrator.tdd.ExecutionSandbox.run",
        return_value=_completed(0, "1 passed"),
    ):
        green = run_tdd_green(cfg, tmp_path, task, red)
    assert green.ok
    assert json.loads(green.output)["red_tests_unchanged"] is True

    test_file.write_text("def test_new_rule():\n    assert True\n", encoding="utf-8")
    with patch(
        "converge_orchestrator.tdd.ExecutionSandbox.run",
        return_value=_completed(0, "1 passed"),
    ):
        weakened = run_tdd_green(cfg, tmp_path, task, red)
    assert not weakened.ok
    assert json.loads(weakened.output)["red_tests_unchanged"] is False


def test_missing_test_gate_and_timeout_cannot_be_red_evidence(tmp_path: Path) -> None:
    cfg = _config(tmp_path)
    task = _behavior_task()
    task.tdd.test_gate = "missing-test-gate"
    missing = run_tdd_baseline(cfg, tmp_path, task)
    assert not missing.ok
    assert "unknown quality gate" in missing.output

    task = _behavior_task()
    with (
        patch(
            "converge_orchestrator.tdd.ExecutionSandbox.run",
            side_effect=__import__("subprocess").TimeoutExpired(cmd=["pytest"], timeout=30),
        ),
        patch("converge_orchestrator.tdd.changed_files", return_value=[]),
    ):
        timed_out = run_tdd_baseline(cfg, tmp_path, task)
    assert not timed_out.ok
    assert json.loads(timed_out.output)["gate_returncode"] == 124
