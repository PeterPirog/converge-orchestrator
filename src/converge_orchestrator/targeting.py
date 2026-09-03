from __future__ import annotations

from typing import Literal

from . import graph as active_graph
from . import workflow as wf
from .compliance import ComplianceEngine
from .config import load_config
from .models import (
    ComplianceSnapshot,
    ProjectConfig,
    Requirement,
    RequirementStatus,
    RequirementVerification,
    TaskEnvelope,
    WorkflowState,
)
from .verification import (
    load_baseline_verification_cache,
    run_requirement_verifiers,
    write_baseline_verification_cache,
)

_STATUS_PRIORITY = {
    RequirementStatus.FAIL: 0,
    RequirementStatus.PARTIAL: 1,
    RequirementStatus.UNVERIFIED: 2,
    RequirementStatus.BLOCKED: 3,
}


def _base_commit(state: WorkflowState) -> str:
    snapshot = (state.get("baseline") or {}).get("repo_scout") or {}
    commit = snapshot.get("base_commit")
    if not commit:
        raise RuntimeError("deterministic target selection requires Repo Scout base_commit")
    return str(commit)


def _baseline_verifications(
    config: ProjectConfig,
    state: WorkflowState,
    requirements: list[Requirement],
) -> tuple[list[RequirementVerification], bool]:
    if not config.requirement_verifiers:
        return [], False
    base_commit = _base_commit(state)
    cached = load_baseline_verification_cache(
        config,
        base_commit=base_commit,
        requirements_sha256=state["requirements_hash"],
    )
    if cached is not None:
        return cached, True
    results = run_requirement_verifiers(
        config,
        config.repo_path,
        requirements,
        writable_cwd=False,
    )
    write_baseline_verification_cache(
        config,
        base_commit=base_commit,
        requirements_sha256=state["requirements_hash"],
        results=results,
    )
    return results, False


def choose_target_requirement(
    requirements: list[Requirement],
    compliance: ComplianceSnapshot,
    config: ProjectConfig,
) -> Requirement | None:
    """Choose one mandatory gap without using an LLM or inferred project intent.

    Deterministically verifiable gaps are preferred because they have an objective completion
    signal and therefore usually require fewer semantic retries/HITL. Stable source order breaks
    ties so the scheduler is reproducible across restarts.
    """
    ranked: list[tuple[tuple[int, int, int], Requirement]] = []
    for index, requirement in enumerate(requirements):
        if requirement.severity != "mandatory":
            continue
        entry = compliance.entries.get(requirement.id)
        status = entry.status if entry is not None else RequirementStatus.UNVERIFIED
        if status == RequirementStatus.PASS:
            continue
        verifier_rank = 0 if config.requirement_verifiers.get(requirement.id) else 1
        status_rank = _STATUS_PRIORITY.get(status, len(_STATUS_PRIORITY))
        ranked.append(((verifier_rank, status_rank, index), requirement))
    if not ranked:
        return None
    ranked.sort(key=lambda item: item[0])
    return ranked[0][1]


def _target_evidence(
    *,
    target: Requirement | None,
    compliance: ComplianceSnapshot,
    verifications: list[RequirementVerification],
    cache_hit: bool,
    base_commit: str,
) -> dict:
    verification_status = {
        item.requirement_id: item.status.value for item in verifications
    }
    return {
        "base_commit": base_commit,
        "cache_hit": cache_hit,
        "target_requirement_id": target.id if target else None,
        "target_status": (
            compliance.entries[target.id].status.value
            if target and target.id in compliance.entries
            else None
        ),
        "deterministic_verification": verification_status,
        "selection_policy": (
            "mandatory_non_pass; verifier_configured_first; "
            "status=fail,partial,unverified,blocked; stable_source_order"
        ),
    }


def targeted_plan(state: WorkflowState) -> WorkflowState:
    """Refresh objective baseline evidence, select one gap, then invoke the existing Planner.

    The Planner receives exactly one immutable requirement. The full authoritative contract remains
    in durable state and is restored before subsequent Builder/reviewer nodes execute.
    """
    cfg = load_config(state["config_path"])
    requirements = wf._requirements(state)
    full_requirements = list(state["requirements"])
    compliance = ComplianceSnapshot.model_validate(state["compliance"])
    verifications, cache_hit = _baseline_verifications(cfg, state, requirements)
    if verifications:
        compliance = ComplianceEngine.apply_verifications(compliance, verifications)

    base_commit = _base_commit(state)
    target = choose_target_requirement(requirements, compliance, cfg)
    evidence = _target_evidence(
        target=target,
        compliance=compliance,
        verifications=verifications,
        cache_hit=cache_hit,
        base_commit=base_commit,
    )
    baseline = dict(state.get("baseline") or {})
    baseline["target_selection"] = evidence
    baseline["requirement_verifications"] = [
        result.model_dump(mode="json") for result in verifications
    ]
    prepared: WorkflowState = {
        **state,
        "baseline": baseline,
        "compliance": compliance.model_dump(mode="json"),
    }
    wf._write_compliance(prepared, compliance)

    store = wf._evidence(prepared)
    iteration = state.get("iteration", 0) + 1
    store.write_json(
        state["run_id"],
        "run",
        f"target-selection-{iteration:04d}.json",
        evidence,
    )
    store.append_event(state["run_id"], "target_selected", evidence)

    if target is None:
        return {
            **prepared,
            "task": None,
            "status": "target_converged",
            "message": "All mandatory requirements have deterministic/durable PASS status.",
        }
    if state.get("iteration", 0) >= cfg.max_iterations:
        return {
            **prepared,
            "status": "iteration_budget_exhausted",
            "message": "Iteration budget exhausted before the next Planner invocation.",
        }

    narrowed: WorkflowState = {
        **prepared,
        "requirements": [target.model_dump(mode="json")],
    }
    planned = active_graph.plan(narrowed)
    task = TaskEnvelope.model_validate(planned["task"])
    if task.requirement_ids != [target.id]:
        raise ValueError(
            "Planner drifted from deterministic target: "
            f"expected requirement_ids=[{target.id!r}], got {task.requirement_ids!r}"
        )
    return {
        **planned,
        "requirements": full_requirements,
        "compliance": compliance.model_dump(mode="json"),
        "baseline": baseline,
    }


def route_after_targeted_plan(
    state: WorkflowState,
) -> Literal["prepare", "human", "end"]:
    if state.get("status") == "target_converged":
        return "end"
    if state.get("status") == "iteration_budget_exhausted":
        return "human"
    return "prepare"
