from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from converge_orchestrator.context import PromptEnvelope
from converge_orchestrator.models import (
    ComplianceEntry,
    ComplianceSnapshot,
    ProjectConfig,
    QualityGate,
    Requirement,
    RequirementStatus,
    RequirementVerification,
    TaskEnvelope,
)
from converge_orchestrator.quality import _baseline_requirement_verifiers
from converge_orchestrator.targeting import (
    choose_target_requirement,
    planner_human_gate,
    route_after_planner_human,
    route_after_targeted_plan,
    targeted_plan,
)
from converge_orchestrator.verification import (
    load_baseline_verification_cache,
    write_baseline_verification_cache,
)


def _config(tmp_path: Path, *, verifiers: dict | None = None) -> ProjectConfig:
    return ProjectConfig(
        repo_path=tmp_path,
        requirements_path=tmp_path / "architecture.md",
        state_dir=tmp_path / ".converge",
        worktree_dir=tmp_path / ".converge" / "worktrees",
        requirement_verifiers=verifiers or {},
        agents={},
        require_spec_read_only=False,
    )


def _requirements() -> list[Requirement]:
    return [
        Requirement(id="ARCH-001", statement="Semantic gap", source="architecture.md:1"),
        Requirement(id="ARCH-002", statement="Verified gap", source="architecture.md:2"),
        Requirement(
            id="ARCH-003",
            statement="Recommended improvement",
            source="architecture.md:3",
            severity="recommended",
        ),
    ]


def _compliance(**statuses: RequirementStatus) -> ComplianceSnapshot:
    entries = {
        requirement_id.replace("_", "-"): ComplianceEntry(
            requirement_id=requirement_id.replace("_", "-"),
            status=status,
        )
        for requirement_id, status in statuses.items()
    }
    return ComplianceSnapshot(entries=entries)


def _state(
    tmp_path: Path,
    compliance: ComplianceSnapshot,
    *,
    planner_attempts: int = 0,
    replan_attempts: int = 0,
) -> dict:
    baseline: dict = {"repo_scout": {"base_commit": "base-sha"}}
    if planner_attempts:
        baseline["planner_control"] = {
            "target_requirement_id": "ARCH-001",
            "attempts": planner_attempts,
            "last_error": "previous deterministic validation failure",
            "last_failure_kind": "contract",
        }
    return {
        "config_path": str(tmp_path / "converge.yaml"),
        "run_id": "run-1",
        "requirements_hash": "spec-sha",
        "requirements": [item.model_dump(mode="json") for item in _requirements()],
        "compliance": compliance.model_dump(mode="json"),
        "baseline": baseline,
        "iteration": 0,
        "replan_attempts": replan_attempts,
    }


def _store() -> SimpleNamespace:
    return SimpleNamespace(write_json=Mock(), append_event=Mock())


def _agent_result(*, ok: bool, output: str) -> SimpleNamespace:
    return SimpleNamespace(ok=ok, output=output, context={"budget_status": "bounded"})


def _valid_task() -> TaskEnvelope:
    return TaskEnvelope(
        id="ARCH-001-1",
        requirement_ids=["ARCH-001"],
        title="Bounded target",
        objective="Close the selected gap",
    )


def test_scheduler_prefers_objectively_verifiable_mandatory_gap(tmp_path: Path) -> None:
    config = _config(
        tmp_path,
        verifiers={
            "ARCH-002": [QualityGate(name="arch", command=["python", "-V"])]
        },
    )
    compliance = _compliance(
        ARCH_001=RequirementStatus.UNVERIFIED,
        ARCH_002=RequirementStatus.FAIL,
        ARCH_003=RequirementStatus.FAIL,
    )

    target = choose_target_requirement(_requirements(), compliance, config)

    assert target is not None
    assert target.id == "ARCH-002"


def test_scheduler_stops_when_all_mandatory_requirements_pass(tmp_path: Path) -> None:
    config = _config(tmp_path)
    compliance = _compliance(
        ARCH_001=RequirementStatus.PASS,
        ARCH_002=RequirementStatus.PASS,
        ARCH_003=RequirementStatus.FAIL,
    )

    assert choose_target_requirement(_requirements(), compliance, config) is None


def test_targeted_plan_skips_model_when_mandatory_contract_is_converged(
    tmp_path: Path,
) -> None:
    config = _config(tmp_path)
    compliance = _compliance(
        ARCH_001=RequirementStatus.PASS,
        ARCH_002=RequirementStatus.PASS,
        ARCH_003=RequirementStatus.FAIL,
    )
    state = _state(tmp_path, compliance)
    store = _store()

    with (
        patch("converge_orchestrator.targeting.load_config", return_value=config),
        patch("converge_orchestrator.targeting.wf._write_compliance"),
        patch("converge_orchestrator.targeting.wf._evidence", return_value=store),
        patch("converge_orchestrator.targeting.OpenCodeAdapter.invoke") as planner,
    ):
        result = targeted_plan(state)  # type: ignore[arg-type]

    planner.assert_not_called()
    assert result["status"] == "target_converged"
    assert result["task"] is None


def test_targeted_plan_sends_only_selected_requirement_and_restores_contract(
    tmp_path: Path,
) -> None:
    config = _config(tmp_path)
    compliance = _compliance(
        ARCH_001=RequirementStatus.FAIL,
        ARCH_002=RequirementStatus.UNVERIFIED,
        ARCH_003=RequirementStatus.UNVERIFIED,
    )
    state = _state(tmp_path, compliance)
    store = _store()
    task = _valid_task()

    def invoke(_adapter, role, prompt, _cwd):  # type: ignore[no-untyped-def]
        assert role == "planner"
        assert isinstance(prompt, PromptEnvelope)
        assert "ARCH-001" in prompt.core
        assert "ARCH-002" not in prompt.core
        assert "ARCH-003" not in prompt.core
        return _agent_result(ok=True, output=task.model_dump_json())

    with (
        patch("converge_orchestrator.targeting.load_config", return_value=config),
        patch("converge_orchestrator.targeting.wf._write_compliance"),
        patch("converge_orchestrator.targeting.wf._evidence", return_value=store),
        patch("converge_orchestrator.targeting.OpenCodeAdapter.invoke", new=invoke),
    ):
        result = targeted_plan(state)  # type: ignore[arg-type]

    assert [item["id"] for item in result["requirements"]] == [
        "ARCH-001",
        "ARCH-002",
        "ARCH-003",
    ]
    assert result["task"]["requirement_ids"] == ["ARCH-001"]
    assert result["iteration"] == 1
    assert result["baseline"]["planner_control"]["attempts"] == 0


def test_invalid_planner_output_retries_without_consuming_iteration(tmp_path: Path) -> None:
    config = _config(tmp_path)
    compliance = _compliance(
        ARCH_001=RequirementStatus.FAIL,
        ARCH_002=RequirementStatus.UNVERIFIED,
        ARCH_003=RequirementStatus.UNVERIFIED,
    )
    state = _state(tmp_path, compliance)
    store = _store()

    with (
        patch("converge_orchestrator.targeting.load_config", return_value=config),
        patch("converge_orchestrator.targeting.wf._write_compliance"),
        patch("converge_orchestrator.targeting.wf._evidence", return_value=store),
        patch(
            "converge_orchestrator.targeting.OpenCodeAdapter.invoke",
            return_value=_agent_result(ok=True, output="not-json"),
        ),
    ):
        result = targeted_plan(state)  # type: ignore[arg-type]

    assert result["status"] == "planner_retry"
    assert result["iteration"] == 0
    assert result["task"] is None
    assert result["baseline"]["planner_control"]["attempts"] == 1
    assert route_after_targeted_plan(result) == "retry"


def test_retry_receives_validation_feedback_but_same_immutable_target(tmp_path: Path) -> None:
    config = _config(tmp_path)
    compliance = _compliance(
        ARCH_001=RequirementStatus.FAIL,
        ARCH_002=RequirementStatus.UNVERIFIED,
        ARCH_003=RequirementStatus.UNVERIFIED,
    )
    state = _state(tmp_path, compliance, planner_attempts=1)
    store = _store()
    task = _valid_task()

    def invoke(_adapter, _role, prompt, _cwd):  # type: ignore[no-untyped-def]
        assert isinstance(prompt, PromptEnvelope)
        feedback = next(
            section for section in prompt.advisory if section.name == "planner validation feedback"
        )
        assert "previous deterministic validation failure" in feedback.text
        assert "ARCH-001" in prompt.core
        assert "ARCH-002" not in prompt.core
        return _agent_result(ok=True, output=task.model_dump_json())

    with (
        patch("converge_orchestrator.targeting.load_config", return_value=config),
        patch("converge_orchestrator.targeting.wf._write_compliance"),
        patch("converge_orchestrator.targeting.wf._evidence", return_value=store),
        patch("converge_orchestrator.targeting.OpenCodeAdapter.invoke", new=invoke),
    ):
        result = targeted_plan(state)  # type: ignore[arg-type]

    assert result["status"] == "planned"
    assert result["task"]["requirement_ids"] == ["ARCH-001"]


def test_second_contract_failure_uses_replan_before_human(tmp_path: Path) -> None:
    config = _config(tmp_path)
    compliance = _compliance(
        ARCH_001=RequirementStatus.FAIL,
        ARCH_002=RequirementStatus.UNVERIFIED,
        ARCH_003=RequirementStatus.UNVERIFIED,
    )
    state = _state(tmp_path, compliance, planner_attempts=1, replan_attempts=0)
    store = _store()
    wrong = TaskEnvelope(
        id="wrong",
        requirement_ids=["ARCH-002"],
        title="Drift",
        objective="Switch target",
    )

    with (
        patch("converge_orchestrator.targeting.load_config", return_value=config),
        patch("converge_orchestrator.targeting.wf._write_compliance"),
        patch("converge_orchestrator.targeting.wf._evidence", return_value=store),
        patch(
            "converge_orchestrator.targeting.OpenCodeAdapter.invoke",
            return_value=_agent_result(ok=True, output=wrong.model_dump_json()),
        ),
    ):
        result = targeted_plan(state)  # type: ignore[arg-type]

    assert result["status"] == "planner_replan_required"
    assert result["baseline"]["planner_control"]["attempts"] == 0
    assert route_after_targeted_plan(result) == "replan"


def test_repeated_execution_failure_escalates_without_wasting_scout_replans(
    tmp_path: Path,
) -> None:
    config = _config(tmp_path)
    compliance = _compliance(
        ARCH_001=RequirementStatus.FAIL,
        ARCH_002=RequirementStatus.UNVERIFIED,
        ARCH_003=RequirementStatus.UNVERIFIED,
    )
    state = _state(tmp_path, compliance, planner_attempts=1, replan_attempts=0)
    store = _store()

    with (
        patch("converge_orchestrator.targeting.load_config", return_value=config),
        patch("converge_orchestrator.targeting.wf._write_compliance"),
        patch("converge_orchestrator.targeting.wf._evidence", return_value=store),
        patch(
            "converge_orchestrator.targeting.OpenCodeAdapter.invoke",
            return_value=_agent_result(ok=False, output="provider unavailable"),
        ),
    ):
        result = targeted_plan(state)  # type: ignore[arg-type]

    assert result["status"] == "planner_human_required"
    assert result["replan_attempts"] == 0
    assert route_after_targeted_plan(result) == "human"


def test_planner_human_gate_cannot_edit_or_replace_deterministic_target() -> None:
    state = {
        "baseline": {
            "target_selection": {"target_requirement_id": "ARCH-001"},
            "planner_control": {
                "target_requirement_id": "ARCH-001",
                "attempts": 2,
                "last_error": "bad envelope",
                "last_failure_kind": "contract",
            },
        },
        "replan_attempts": 2,
    }
    captured: dict = {}

    def fake_interrupt(payload):  # type: ignore[no-untyped-def]
        captured.update(payload)
        return {"action": "retry"}

    with patch("converge_orchestrator.targeting.interrupt", side_effect=fake_interrupt):
        result = planner_human_gate(state)  # type: ignore[arg-type]

    assert captured["allowed"] == ["retry", "stop"]
    assert captured["target"] == "ARCH-001"
    assert result["replan_attempts"] == 0
    assert result["baseline"]["planner_control"]["attempts"] == 0
    assert route_after_planner_human(result) == "retry"


def test_baseline_verification_cache_is_invalidated_by_policy_change(tmp_path: Path) -> None:
    gate = QualityGate(name="arch", command=["python", "-V"])
    config = _config(tmp_path, verifiers={"ARCH-001": [gate]})
    result = RequirementVerification(
        requirement_id="ARCH-001",
        status=RequirementStatus.PASS,
        evidence=["pass"],
    )
    write_baseline_verification_cache(
        config,
        base_commit="base-sha",
        requirements_sha256="spec-sha",
        results=[result],
    )

    cached = load_baseline_verification_cache(
        config,
        base_commit="base-sha",
        requirements_sha256="spec-sha",
    )
    assert cached == [result]

    changed = _config(
        tmp_path,
        verifiers={
            "ARCH-001": [QualityGate(name="arch", command=["python", "--version"])]
        },
    )
    assert (
        load_baseline_verification_cache(
            changed,
            base_commit="base-sha",
            requirements_sha256="spec-sha",
        )
        is None
    )


def test_quality_reuses_cached_baseline_without_rerunning_verifiers(tmp_path: Path) -> None:
    config = _config(
        tmp_path,
        verifiers={
            "ARCH-001": [QualityGate(name="arch", command=["python", "-V"])]
        },
    )
    cached = [
        RequirementVerification(
            requirement_id="ARCH-001",
            status=RequirementStatus.FAIL,
        )
    ]
    contract = SimpleNamespace(source=SimpleNamespace(sha256="spec-sha"))

    with (
        patch("converge_orchestrator.quality.current_head", return_value="base-sha"),
        patch(
            "converge_orchestrator.quality.load_baseline_verification_cache",
            return_value=cached,
        ),
        patch("converge_orchestrator.quality.run_requirement_verifiers") as execute,
    ):
        results, cache_hit = _baseline_requirement_verifiers(config, contract)

    execute.assert_not_called()
    assert cache_hit is True
    assert results == cached
