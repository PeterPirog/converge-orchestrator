from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

import pytest

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
from converge_orchestrator.targeting import choose_target_requirement, targeted_plan
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
    requirements = _requirements()
    compliance = _compliance(
        ARCH_001=RequirementStatus.PASS,
        ARCH_002=RequirementStatus.PASS,
        ARCH_003=RequirementStatus.FAIL,
    )
    state = {
        "config_path": str(tmp_path / "converge.yaml"),
        "run_id": "run-1",
        "requirements_hash": "spec-sha",
        "requirements": [item.model_dump(mode="json") for item in requirements],
        "compliance": compliance.model_dump(mode="json"),
        "baseline": {"repo_scout": {"base_commit": "base-sha"}},
        "iteration": 0,
    }
    store = SimpleNamespace(write_json=Mock(), append_event=Mock())

    with (
        patch("converge_orchestrator.targeting.load_config", return_value=config),
        patch("converge_orchestrator.targeting.wf._write_compliance"),
        patch("converge_orchestrator.targeting.wf._evidence", return_value=store),
        patch("converge_orchestrator.targeting.active_graph.plan") as planner,
    ):
        result = targeted_plan(state)  # type: ignore[arg-type]

    planner.assert_not_called()
    assert result["status"] == "target_converged"
    assert result["task"] is None


def test_targeted_plan_exposes_only_selected_requirement_then_restores_contract(
    tmp_path: Path,
) -> None:
    config = _config(tmp_path)
    requirements = _requirements()
    compliance = _compliance(
        ARCH_001=RequirementStatus.FAIL,
        ARCH_002=RequirementStatus.UNVERIFIED,
        ARCH_003=RequirementStatus.UNVERIFIED,
    )
    state = {
        "config_path": str(tmp_path / "converge.yaml"),
        "run_id": "run-1",
        "requirements_hash": "spec-sha",
        "requirements": [item.model_dump(mode="json") for item in requirements],
        "compliance": compliance.model_dump(mode="json"),
        "baseline": {"repo_scout": {"base_commit": "base-sha"}},
        "iteration": 0,
    }
    task = TaskEnvelope(
        id="ARCH-001-1",
        requirement_ids=["ARCH-001"],
        title="Bounded target",
        objective="Close the selected gap",
    )
    store = SimpleNamespace(write_json=Mock(), append_event=Mock())

    def fake_plan(narrowed):  # type: ignore[no-untyped-def]
        assert [item["id"] for item in narrowed["requirements"]] == ["ARCH-001"]
        return {**narrowed, "task": task.model_dump(mode="json"), "status": "planned"}

    with (
        patch("converge_orchestrator.targeting.load_config", return_value=config),
        patch("converge_orchestrator.targeting.wf._write_compliance"),
        patch("converge_orchestrator.targeting.wf._evidence", return_value=store),
        patch("converge_orchestrator.targeting.active_graph.plan", side_effect=fake_plan),
    ):
        result = targeted_plan(state)  # type: ignore[arg-type]

    assert [item["id"] for item in result["requirements"]] == [
        "ARCH-001",
        "ARCH-002",
        "ARCH-003",
    ]
    assert result["baseline"]["target_selection"]["target_requirement_id"] == "ARCH-001"


def test_targeted_plan_rejects_planner_requirement_drift(tmp_path: Path) -> None:
    config = _config(tmp_path)
    requirements = _requirements()
    compliance = _compliance(
        ARCH_001=RequirementStatus.FAIL,
        ARCH_002=RequirementStatus.UNVERIFIED,
        ARCH_003=RequirementStatus.UNVERIFIED,
    )
    state = {
        "config_path": str(tmp_path / "converge.yaml"),
        "run_id": "run-1",
        "requirements_hash": "spec-sha",
        "requirements": [item.model_dump(mode="json") for item in requirements],
        "compliance": compliance.model_dump(mode="json"),
        "baseline": {"repo_scout": {"base_commit": "base-sha"}},
        "iteration": 0,
    }
    wrong_task = TaskEnvelope(
        id="wrong",
        requirement_ids=[],
        title="Drift",
        objective="Avoid the selected requirement",
    )
    store = SimpleNamespace(write_json=Mock(), append_event=Mock())

    with (
        patch("converge_orchestrator.targeting.load_config", return_value=config),
        patch("converge_orchestrator.targeting.wf._write_compliance"),
        patch("converge_orchestrator.targeting.wf._evidence", return_value=store),
        patch(
            "converge_orchestrator.targeting.active_graph.plan",
            return_value={**state, "task": wrong_task.model_dump(mode="json")},
        ),
    ):
        with pytest.raises(ValueError, match="Planner drifted"):
            targeted_plan(state)  # type: ignore[arg-type]


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
