"""V29 release-gate regression tests for TDD contract autonomous recovery.

V29 (run ``66c121c20d2e4ff2a9892f05d587bf06``) proved that a Planner-produced Task
Envelope with ``tdd.mode="required"`` and a descriptive ``tdd.test_gate`` string
survived Planner contract validation, failed only inside ``tdd_baseline`` and was then
routed to the ordinary ``tdd_evidence_failure`` HITL because the global replan counter
had already been consumed by unrelated CI replans. No RED command ever ran and the
Builder was never invoked for that plan.

These tests prove the corrected behavior:

- unknown TDD gate references are rejected deterministically at Planner contract
  validation time through the existing bounded contract machinery;
- TDD-evidence failures use their own bounded recovery budget, independent of the
  CI/review replan counter;
- the budget is finite, durable across checkpoint restore, and reset only at
  legitimate lifecycle boundaries;
- the Builder is never invoked and no PASS is produced without valid required RED.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

import pytest
from langgraph.errors import GraphInterrupt
from langgraph.types import Command

from converge_orchestrator.graph import (
    route_after_tdd_baseline,
    route_after_tdd_red,
)
from converge_orchestrator.graph_service import build_graph
from converge_orchestrator.models import (
    CIResult,
    ProjectConfig,
    PullRequestInfo,
    TaskEnvelope,
)
from converge_orchestrator.persistence import open_checkpointer
from converge_orchestrator.spec import compile_contract
from converge_orchestrator.targeting import route_after_targeted_plan, targeted_plan
from converge_orchestrator.tdd import invalid_tdd_gate_reference
from converge_orchestrator.workflow import record_tdd_evidence_feedback, replan

_V29_STYLE_GATE = (
    "python -m pytest -q tests/test_shared_tools_fake_terminal.py (same gate as CI)"
)


def _config(tmp_path: Path, *, gates: list | None = None) -> ProjectConfig:
    return ProjectConfig(
        repo_path=tmp_path,
        requirements_path=tmp_path / "architecture.md",
        state_dir=tmp_path / ".converge",
        worktree_dir=tmp_path / ".converge" / "worktrees",
        quality_gates=gates or [],
        agents={},
        require_spec_read_only=False,
    )


def _store() -> SimpleNamespace:
    return SimpleNamespace(write_json=Mock(), append_event=Mock())


def _agent_result(*, ok: bool, output: str, context: dict | None = None) -> SimpleNamespace:
    payload: dict = {"budget_status": "bounded"}
    if context is not None:
        payload.update(context)
    return SimpleNamespace(ok=ok, output=output, context=payload)


def _task(*, test_gate: str | None = None, **overrides) -> dict:
    payload = {
        "id": "ARCH-001-0038",
        "requirement_ids": ["ARCH-001"],
        "title": "Bounded TDD task",
        "objective": "Add one red-first test",
        "allowed_paths": ["tests/**"],
        "acceptance": ["New test pins the contract"],
        "max_diff_lines": 40,
        "risk": "low",
        "risk_flags": [],
        "change_kind": "test_only",
        "tdd": {
            "mode": "required",
            "test_paths": ["tests/**"],
            "test_gate": test_gate,
            "expected_failure_pattern": "AssertionError: ARCH-001 marker missing",
            "rationale": "red-first evidence",
        },
    }
    payload.update(overrides)
    return payload


def _plan_state(tmp_path: Path, *, attempts: int = 0, replan_attempts: int = 0) -> dict:
    (tmp_path / "tests").mkdir(exist_ok=True)
    (tmp_path / "tests" / "test_contract.py").write_text(
        "def test_placeholder():\n    assert True\n", encoding="utf-8"
    )
    baseline: dict = {"repo_scout": {"base_commit": "base-sha"}}
    if attempts:
        baseline["planner_control"] = {
            "target_requirement_id": "ARCH-001",
            "attempts": attempts,
            "last_error": "previous deterministic validation failure",
            "last_failure_kind": "contract",
        }
    return {
        "config_path": str(tmp_path / "converge.yaml"),
        "run_id": "run-1",
        "requirements_hash": "spec-sha",
        "requirements": [
            {
                "id": "ARCH-001",
                "statement": "Semantic gap",
                "source": "architecture.md:1",
                "severity": "mandatory",
            }
        ],
        "compliance": {
            "entries": {
                "ARCH-001": {
                    "requirement_id": "ARCH-001",
                    "status": "fail",
                    "evidence": [],
                }
            },
            "mandatory_regressions": 0,
        },
        "baseline": baseline,
        "iteration": 0,
        "replan_attempts": replan_attempts,
    }


def _gate_result(name: str, ok: bool) -> dict:
    return {"name": name, "ok": ok, "required": True, "returncode": 0 if ok else 1, "output": "{}"}


# ---------------------------------------------------------------------------
# TEST A — unknown TDD gate caught during Planner validation
# ---------------------------------------------------------------------------


def test_unknown_tdd_gate_is_rejected_at_plan_time_and_never_reaches_baseline(
    tmp_path: Path,
) -> None:
    config = _config(
        tmp_path,
        gates=[{"name": "target-test-suite", "command": ["python", "-m", "pytest"]}],
    )
    state = _plan_state(tmp_path)
    store = _store()
    calls: list[str] = []

    def fake_invoke(_adapter, role, _prompt, _cwd):
        calls.append(role)
        return _agent_result(ok=True, output=json.dumps(_task(test_gate=_V29_STYLE_GATE)))

    sandbox = Mock(side_effect=AssertionError("RED/baseline command must not run at plan time"))

    with (
        patch("converge_orchestrator.targeting.load_config", return_value=config),
        patch("converge_orchestrator.targeting.wf._write_compliance"),
        patch("converge_orchestrator.targeting.wf._evidence", return_value=store),
        patch("converge_orchestrator.targeting.OpenCodeAdapter.invoke", new=fake_invoke),
        patch("converge_orchestrator.tdd.ExecutionSandbox.run", new=sandbox),
    ):
        result = targeted_plan(state)  # type: ignore[arg-type]

    assert calls == ["planner"]
    assert result["status"] == "planner_retry"
    assert result["task"] is None
    control = result["baseline"]["planner_control"]
    assert control["attempts"] == 1
    assert "TDD requested unknown quality gate" in control["last_error"]
    assert _V29_STYLE_GATE in control["last_error"]
    assert control["last_failure_kind"] == "contract"
    sandbox.assert_not_called()
    assert route_after_targeted_plan(result) == "retry"


def test_planner_retry_receives_exact_gate_feedback_and_correction_recovers(
    tmp_path: Path,
) -> None:
    config = _config(
        tmp_path,
        gates=[{"name": "target-test-suite", "command": ["python", "-m", "pytest"]}],
    )
    state = _plan_state(tmp_path)
    store = _store()
    prompts: list = []

    def fake_invoke(_adapter, role, prompt, _cwd):
        prompts.append(prompt)
        if len(prompts) == 1:
            return _agent_result(ok=True, output=json.dumps(_task(test_gate=_V29_STYLE_GATE)))
        return _agent_result(ok=True, output=json.dumps(_task(test_gate=None)))

    with (
        patch("converge_orchestrator.targeting.load_config", return_value=config),
        patch("converge_orchestrator.targeting.wf._write_compliance"),
        patch("converge_orchestrator.targeting.wf._evidence", return_value=store),
        patch("converge_orchestrator.targeting.OpenCodeAdapter.invoke", new=fake_invoke),
    ):
        rejected = targeted_plan(state)  # type: ignore[arg-type]
        assert rejected["status"] == "planner_retry"
        corrected = targeted_plan(rejected)  # type: ignore[arg-type]

    feedback = next(
        section
        for section in prompts[1].advisory
        if section.name == "planner validation feedback"
    )
    assert "TDD requested unknown quality gate" in feedback.text
    assert "Correct only this contract error" in feedback.text
    assert corrected["status"] == "planned"
    assert corrected["task"]["tdd"]["test_gate"] is None


def test_repeated_unknown_gate_failure_uses_bounded_contract_replan(tmp_path: Path) -> None:
    config = _config(tmp_path)
    state = _plan_state(tmp_path, attempts=1)
    store = _store()

    with (
        patch("converge_orchestrator.targeting.load_config", return_value=config),
        patch("converge_orchestrator.targeting.wf._write_compliance"),
        patch("converge_orchestrator.targeting.wf._evidence", return_value=store),
        patch(
            "converge_orchestrator.targeting.OpenCodeAdapter.invoke",
            return_value=_agent_result(
                ok=True, output=json.dumps(_task(test_gate=_V29_STYLE_GATE))
            ),
        ),
    ):
        result = targeted_plan(state)  # type: ignore[arg-type]

    assert result["status"] == "planner_replan_required"
    assert route_after_targeted_plan(result) == "replan"


def test_unknown_gate_reference_helper_matches_production_resolution(tmp_path: Path) -> None:
    config = _config(
        tmp_path,
        gates=[{"name": "target-test-suite", "command": ["python", "-m", "pytest"]}],
    )
    task = TaskEnvelope.model_validate(_task(test_gate="target-test-suite"))
    assert invalid_tdd_gate_reference(config, tmp_path, task) is None

    unknown = TaskEnvelope.model_validate(_task(test_gate=_V29_STYLE_GATE))
    error = invalid_tdd_gate_reference(config, tmp_path, unknown)
    assert error == f"TDD requested unknown quality gate: {_V29_STYLE_GATE}"

    null_gate = TaskEnvelope.model_validate(_task(test_gate=None))
    assert invalid_tdd_gate_reference(config, tmp_path, null_gate) is None


# ---------------------------------------------------------------------------
# TEST B — CI replan budget independence
# ---------------------------------------------------------------------------


def test_baseline_failure_keeps_tdd_budget_when_ci_replans_are_exhausted(
    tmp_path: Path,
) -> None:
    config = _config(tmp_path)
    state = {
        "config_path": "converge.yaml",
        "tdd_baseline_result": _gate_result("tdd_baseline", ok=False),
        "replan_attempts": 2,
        "tdd_replan_attempts": 0,
    }
    with patch("converge_orchestrator.graph.load_config", return_value=config):
        assert route_after_tdd_baseline(state) == "replan"


def test_red_failure_keeps_tdd_budget_when_ci_replans_are_exhausted(
    tmp_path: Path,
) -> None:
    config = _config(tmp_path)
    state = {
        "config_path": "converge.yaml",
        "tdd_red_result": _gate_result("tdd_red", ok=False),
        "tdd_red_attempts": 3,
        "replan_attempts": 2,
        "tdd_replan_attempts": 0,
    }
    with patch("converge_orchestrator.graph.load_config", return_value=config):
        assert route_after_tdd_red(state) == "replan"


def test_replan_increments_tdd_counter_only_for_tdd_failure_class(tmp_path: Path) -> None:
    store = _store()
    base_state = {
        "config_path": "converge.yaml",
        "run_id": "run-1",
        "task": {"id": "ARCH-001-0038"},
        "tdd_replan_attempts": 1,
        "replan_attempts": 2,
    }

    with patch("converge_orchestrator.workflow._evidence", return_value=store), patch(
        "converge_orchestrator.workflow._discard_current_workspace"
    ):
        tdd_result = replan({**base_state, "status": "tdd_baseline_unavailable"})
        assert tdd_result["tdd_replan_attempts"] == 2
        assert tdd_result["replan_attempts"] == 2

        other_result = replan({**base_state, "status": "ci_fail"})
        assert other_result["tdd_replan_attempts"] == 1
        assert other_result["replan_attempts"] == 3

    replan_event = store.append_event.call_args_list[0]
    assert replan_event.args[1] == "replan"
    assert replan_event.args[2]["failure_class"] == "tdd_evidence"


def test_tdd_evidence_feedback_shape_and_target_binding(tmp_path: Path) -> None:
    task = TaskEnvelope.model_validate(_task(test_gate=None))
    result = record_tdd_evidence_feedback(
        {"baseline": {"repo_scout": {"base_commit": "base-sha"}}},
        task,
        '{"reason": "usable TDD baseline evidence is missing"}',
    )
    control = result["baseline"]["planner_control"]
    assert control["target_requirement_id"] == "ARCH-001"
    assert control["last_failure_kind"] == "tdd_evidence"
    assert control["last_error"].startswith("TDD evidence gate rejected the plan contract")
    assert "usable TDD baseline evidence is missing" in control["last_error"]


def test_tdd_evidence_feedback_reaches_next_planner_prompt(tmp_path: Path) -> None:
    config = _config(
        tmp_path,
        gates=[{"name": "target-test-suite", "command": ["python", "-m", "pytest"]}],
    )
    state = _plan_state(tmp_path)
    state["baseline"]["planner_control"] = {
        "target_requirement_id": "ARCH-001",
        "attempts": 0,
        "last_error": (
            "TDD evidence gate rejected the plan contract: "
            '{"reason": "usable TDD baseline evidence is missing"}'
        ),
        "last_failure_kind": "tdd_evidence",
    }
    store = _store()
    prompts: list = []

    def fake_invoke(_adapter, role, prompt, _cwd):
        prompts.append(prompt)
        return _agent_result(
            ok=True, output=json.dumps(_task(test_gate="target-test-suite"))
        )

    with (
        patch("converge_orchestrator.targeting.load_config", return_value=config),
        patch("converge_orchestrator.targeting.wf._write_compliance"),
        patch("converge_orchestrator.targeting.wf._evidence", return_value=store),
        patch("converge_orchestrator.targeting.OpenCodeAdapter.invoke", new=fake_invoke),
    ):
        result = targeted_plan(state)  # type: ignore[arg-type]

    feedback = next(
        section
        for section in prompts[0].advisory
        if section.name == "planner validation feedback"
    )
    assert "TDD evidence gate rejected the plan contract" in feedback.text
    assert "usable TDD baseline evidence is missing" in feedback.text
    assert "Correct only this contract error" in feedback.text
    assert result["status"] == "planned"


# ---------------------------------------------------------------------------
# TEST C — TDD budget is finite
# ---------------------------------------------------------------------------


def test_exhausted_tdd_budget_routes_fail_closed_to_human(tmp_path: Path) -> None:
    config = _config(tmp_path)
    state = {
        "config_path": "converge.yaml",
        "tdd_baseline_result": _gate_result("tdd_baseline", ok=False),
        "replan_attempts": 0,
        "tdd_replan_attempts": 2,
    }
    with patch("converge_orchestrator.graph.load_config", return_value=config):
        assert route_after_tdd_baseline(state) == "human"


def test_tdd_replan_budget_resets_only_at_lifecycle_boundaries(tmp_path: Path) -> None:
    store = _store()
    state = {
        "config_path": "converge.yaml",
        "run_id": "run-1",
        "task": {"id": "ARCH-001-0038"},
        "status": "tdd_red_failed",
        "tdd_replan_attempts": 2,
        "replan_attempts": 0,
        "iteration": 1,
    }
    with patch("converge_orchestrator.workflow._evidence", return_value=store), patch(
        "converge_orchestrator.workflow._discard_current_workspace"
    ):
        result = replan(state)  # type: ignore[arg-type]
    assert result["tdd_replan_attempts"] == 3
    assert result["status"] == "replanning"


# ---------------------------------------------------------------------------
# TEST D / E — valid and null gate contracts unchanged
# ---------------------------------------------------------------------------


def test_valid_registered_gate_name_plans_normally(tmp_path: Path) -> None:
    config = _config(
        tmp_path,
        gates=[{"name": "target-test-suite", "command": ["python", "-m", "pytest"]}],
    )
    state = _plan_state(tmp_path)
    store = _store()

    with (
        patch("converge_orchestrator.targeting.load_config", return_value=config),
        patch("converge_orchestrator.targeting.wf._write_compliance"),
        patch("converge_orchestrator.targeting.wf._evidence", return_value=store),
        patch(
            "converge_orchestrator.targeting.OpenCodeAdapter.invoke",
            return_value=_agent_result(
                ok=True,
                output=json.dumps(_task(test_gate="target-test-suite")),
            ),
        ),
    ):
        result = targeted_plan(state)  # type: ignore[arg-type]

    assert result["status"] == "planned"
    assert result["task"]["tdd"]["test_gate"] == "target-test-suite"


def test_null_gate_default_selection_semantics_unchanged(tmp_path: Path) -> None:
    config = _config(tmp_path)
    state = _plan_state(tmp_path)
    store = _store()

    with (
        patch("converge_orchestrator.targeting.load_config", return_value=config),
        patch("converge_orchestrator.targeting.wf._write_compliance"),
        patch("converge_orchestrator.targeting.wf._evidence", return_value=store),
        patch(
            "converge_orchestrator.targeting.OpenCodeAdapter.invoke",
            return_value=_agent_result(ok=True, output=json.dumps(_task(test_gate=None))),
        ),
    ):
        result = targeted_plan(state)  # type: ignore[arg-type]

    assert result["status"] == "planned"


def test_legacy_plan_node_rejects_unknown_gate_fail_closed(tmp_path: Path) -> None:
    from converge_orchestrator.graph import plan as legacy_plan

    config = _config(tmp_path)
    state = _plan_state(tmp_path)
    state["iteration"] = 0
    store = _store()

    with (
        patch("converge_orchestrator.graph.load_config", return_value=config),
        patch("converge_orchestrator.graph.wf._evidence", return_value=store),
        patch(
            "converge_orchestrator.graph.OpenCodeAdapter.invoke",
            return_value=_agent_result(
                ok=True, output=json.dumps(_task(test_gate=_V29_STYLE_GATE))
            ),
        ),
        pytest.raises(ValueError, match="TDD requested unknown quality gate"),
    ):
        legacy_plan(state)  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# TEST F / G — durability, budget exhaustion and safety invariants end-to-end
# ---------------------------------------------------------------------------


def _e2e_repository(tmp_path: Path) -> tuple[Path, Path]:
    origin = tmp_path / "origin.git"
    subprocess.run(
        ["git", "init", "--bare", "--initial-branch=main", str(origin)],
        check=True,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
    )
    repo = tmp_path / "repo"
    subprocess.run(
        ["git", "clone", str(origin), str(repo)],
        check=True,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
    )
    for args in (
        ["config", "user.email", "converge@example.invalid"],
        ["config", "user.name", "Converge E2E"],
    ):
        subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True, text=True)
    (repo / "README.md").write_text("baseline\n", encoding="utf-8")
    subprocess.run(
        ["git", "add", "README.md"], cwd=repo, check=True, capture_output=True, text=True
    )
    subprocess.run(
        ["git", "commit", "-m", "baseline"],
        cwd=repo,
        check=True,
        capture_output=True,
        text=True,
    )
    subprocess.run(
        ["git", "push", "-u", "origin", "main"],
        cwd=repo,
        check=True,
        capture_output=True,
        text=True,
    )
    return repo, origin


def _e2e_config(tmp_path: Path, repo: Path, requirements: Path) -> Path:
    config = {
        "version": 1,
        "project": {
            "repo_path": str(repo),
            "requirements_path": str(requirements),
            "state_dir": str(tmp_path / "state"),
            "worktree_dir": str(tmp_path / "worktrees"),
            "require_spec_read_only": False,
        },
        "github": {"repo": "example/convergence-target", "auto_merge": False},
        "agents": {
            "planner": {"agent": "e2e-planner", "model": "fake/planner"},
            "builder": {"agent": "e2e-builder", "model": "fake/builder"},
        },
        "quality": {
            "auto_discover": False,
            "gates": [
                {
                    "name": "target-test-suite",
                    "command": [
                        sys.executable,
                        "-c",
                        "import time; time.sleep(5)",
                    ],
                    "required": True,
                    "timeout_seconds": 1,
                }
            ],
        },
        "workflow": {"max_repair_attempts": 1, "max_replans": 2, "max_iterations": 8},
    }
    config_path = tmp_path / "converge.yaml"
    config_path.write_text(json.dumps(config, indent=2) + "\n", encoding="utf-8")
    return config_path


def test_v29_class_failure_is_bounded_durable_and_builder_free(tmp_path: Path) -> None:
    """Exact V29 failure class replayed after the fix.

    The Planner keeps emitting a TDD contract whose baseline gate can never produce
    usable evidence (gate always times out). The run must:

    - never invoke the Builder (no RED authority, no implementation work);
    - never run a RED phase (``tdd_red_result`` stays absent);
    - consume only the TDD-evidence budget (global replan counter untouched);
    - end fail-closed at the explicit ``tdd_human`` boundary with the exact cause;
    - gain no extra attempts from a controller restart (durable counter).
    """
    repo, _origin = _e2e_repository(tmp_path)
    requirements = tmp_path / "architecture.md"
    requirements.write_text(
        "# Goal\n"
        "ARCH-001 The repository must pin the deterministic command simulation contract.\n",
        encoding="utf-8",
    )
    requirement_id = compile_contract(requirements).requirements[0].id
    config_path = _e2e_config(tmp_path, repo, requirements)
    agent_calls: list[str] = []

    def fake_invoke(_adapter, role, _prompt, _cwd):
        agent_calls.append(role)
        assert role == "planner"
        task = _task(
            id="E2E-001",
            requirement_ids=[requirement_id],
            title="Pin simulation contract",
            objective="Add the red-first contract test.",
            allowed_paths=["README.md"],
            tdd={
                "mode": "required",
                "test_paths": ["tests/**"],
                "test_gate": "target-test-suite",
                "expected_failure_pattern": "AssertionError: E2E marker missing",
                "rationale": "red-first evidence",
            },
        )
        return _agent_result(ok=True, output=json.dumps(task), context=None)

    class FakeGitHubAdapter:
        def __init__(self, config):
            self.config = config

        def ensure_pull_request(self, *, head, base, title, body):
            del base, title, body
            return PullRequestInfo(
                number=17,
                url="https://github.invalid/example/convergence-target/pull/17",
                head_sha=head,
                state="open",
            )

        def ci_status(self, head_sha):
            return CIResult(status="pending", head_sha=head_sha, checks=[])

        def close_pull_request(self, number):
            del number

    state_dir = tmp_path / "state"
    state_dir.mkdir(parents=True, exist_ok=True)
    graph_config = {"configurable": {"thread_id": "v29-replay-thread"}}
    initial = {
        "project_id": "v29-replay",
        "config_path": str(config_path),
        "run_id": "v29-replay-run",
        "thread_id": "v29-replay-thread",
    }
    try:
        checkpointer, db = open_checkpointer(state_dir)
        graph = build_graph(checkpointer=checkpointer)
        with (
            patch("converge_orchestrator.opencode.OpenCodeAdapter.invoke", new=fake_invoke),
            patch("converge_orchestrator.workflow.GitHubAdapter", FakeGitHubAdapter),
            patch("converge_orchestrator.ci.GitHubAdapter", FakeGitHubAdapter),
        ):
            try:
                graph.invoke(initial, config=graph_config)
            except GraphInterrupt:
                pass
            snapshot = graph.get_state(graph_config)
            assert snapshot.next == ("tdd_human",)
            values = dict(snapshot.values)
            assert values["status"] == "tdd_baseline_unavailable"
            assert values["tdd_replan_attempts"] == 2
            assert values["replan_attempts"] == 0
            assert values["tdd_red_result"] is None
            assert agent_calls == ["planner"] * 3

            interrupts = list(getattr(snapshot, "interrupts", ()) or ())
            for task in getattr(snapshot, "tasks", ()) or ():
                interrupts.extend(getattr(task, "interrupts", ()) or ())
            payloads = [
                item.value if isinstance(item.value, dict) else {"kind": "unknown"}
                for item in interrupts
            ]
            failure = next(
                payload for payload in payloads if payload.get("kind") == "tdd_evidence_failure"
            )
            assert failure["baseline"]["ok"] is False
            assert failure["red"] is None
            assert failure["allowed"] == ["replan", "stop"]
    finally:
        db.close()

    # Simulate a controller process restart: fresh graph/checkpointer handles from the
    # same durable storage must observe the same exhausted budget (no attempt reset).
    checkpointer, db = open_checkpointer(state_dir)
    try:
        graph = build_graph(checkpointer=checkpointer)
        snapshot = graph.get_state(graph_config)
        assert snapshot.next == ("tdd_human",)
        values = dict(snapshot.values)
        assert values["tdd_replan_attempts"] == 2
        assert values["replan_attempts"] == 0

        with (
            patch("converge_orchestrator.opencode.OpenCodeAdapter.invoke", new=fake_invoke),
            patch("converge_orchestrator.workflow.GitHubAdapter", FakeGitHubAdapter),
            patch("converge_orchestrator.ci.GitHubAdapter", FakeGitHubAdapter),
        ):
            stopped = graph.invoke(
                Command(resume={"action": "stop"}),
                config=graph_config,
            )
        assert stopped["status"] == "stopped"
        assert stopped["tdd_replan_attempts"] == 2
    finally:
        db.close()