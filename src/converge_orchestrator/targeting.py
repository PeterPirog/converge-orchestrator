from __future__ import annotations

import json
from typing import Literal

from langgraph.types import interrupt

from . import workflow as wf
from .compliance import ComplianceEngine
from .config import load_config
from .context import AdvisorySection, PromptEnvelope, build_working_memory
from .models import (
    ComplianceSnapshot,
    ProjectConfig,
    Requirement,
    RequirementStatus,
    RequirementVerification,
    TaskEnvelope,
    WorkflowState,
)
from .opencode import OpenCodeAdapter
from .prompts import planner_prompt
from .verification import (
    load_baseline_verification_cache,
    run_requirement_verifiers,
    write_baseline_verification_cache,
)

_MAX_PLAN_ATTEMPTS = 2
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


def _planner_control(baseline: dict, target_id: str) -> dict:
    raw = baseline.get("planner_control")
    if not isinstance(raw, dict) or raw.get("target_requirement_id") != target_id:
        return {
            "target_requirement_id": target_id,
            "attempts": 0,
            "last_error": None,
            "last_failure_kind": None,
        }
    return dict(raw)


def _planner_advisory(
    state: WorkflowState,
    baseline: dict,
    control: dict,
) -> tuple[list[AdvisorySection], dict]:
    snapshot = baseline.get("repo_scout")
    memory = build_working_memory(state)
    advisory: list[AdvisorySection] = []
    if snapshot:
        advisory.append(
            AdvisorySection(
                "repository scout snapshot",
                json.dumps(snapshot, ensure_ascii=False, indent=2),
            )
        )
    advisory.append(
        AdvisorySection(
            "working memory",
            json.dumps(memory, ensure_ascii=False, indent=2),
        )
    )
    last_error = control.get("last_error")
    if last_error:
        advisory.append(
            AdvisorySection(
                "planner validation feedback",
                (
                    "The previous plan was deterministically rejected. Correct only this contract "
                    "error; the immutable target is unchanged:\n"
                    f"{str(last_error)[:2000]}"
                ),
            )
        )
    return advisory, memory


def _planner_failure(
    state: WorkflowState,
    *,
    baseline: dict,
    target: Requirement,
    attempt: int,
    kind: Literal["contract", "execution"],
    error: str,
    output_tail: str = "",
) -> WorkflowState:
    cfg = load_config(state["config_path"])
    clipped_error = error.strip()[:2000]
    control = {
        "target_requirement_id": target.id,
        "attempts": attempt,
        "last_error": clipped_error,
        "last_failure_kind": kind,
    }
    if attempt < _MAX_PLAN_ATTEMPTS:
        status = "planner_retry"
    elif kind == "contract" and state.get("replan_attempts", 0) < cfg.max_replans:
        status = "planner_replan_required"
        control["attempts"] = 0
    else:
        status = "planner_human_required"

    updated_baseline = dict(baseline)
    updated_baseline["planner_control"] = control
    payload = {
        "target_requirement_id": target.id,
        "attempt": attempt,
        "max_attempts": _MAX_PLAN_ATTEMPTS,
        "failure_kind": kind,
        "error": clipped_error,
        "output_tail": output_tail[-2000:],
        "next_status": status,
    }
    store = wf._evidence(state)
    iteration = state.get("iteration", 0) + 1
    store.write_json(
        state["run_id"],
        "run",
        f"planner-validation-{iteration:04d}-attempt-{attempt:02d}.json",
        payload,
    )
    store.append_event(state["run_id"], "planner_rejected", payload)
    return {
        **state,
        "baseline": updated_baseline,
        "task": None,
        "status": status,
        "message": clipped_error,
    }


def _invoke_target_planner(
    state: WorkflowState,
    *,
    baseline: dict,
    target: Requirement,
    control: dict,
) -> WorkflowState:
    cfg = load_config(state["config_path"])
    iteration = state.get("iteration", 0) + 1
    attempt = int(control.get("attempts", 0)) + 1
    narrowed: WorkflowState = {
        **state,
        "requirements": [target.model_dump(mode="json")],
        "baseline": baseline,
    }
    advisory, memory = _planner_advisory(narrowed, baseline, control)
    prompt = PromptEnvelope(
        core=planner_prompt([target], iteration),
        advisory=tuple(advisory),
    )
    store = wf._evidence(state)
    store.write_json(
        state["run_id"],
        "run",
        f"working-memory-{iteration:04d}-plan-{attempt:02d}.json",
        memory,
    )
    result = OpenCodeAdapter(cfg).invoke("planner", prompt, cfg.repo_path)
    if result.context:
        store.write_json(
            state["run_id"],
            "run",
            f"context-plan-{iteration:04d}-attempt-{attempt:02d}.json",
            result.context,
        )
    if not result.ok:
        return _planner_failure(
            state,
            baseline=baseline,
            target=target,
            attempt=attempt,
            kind="execution",
            error=f"Planner execution failed: {result.output[-1200:]}",
            output_tail=result.output,
        )

    try:
        task = TaskEnvelope.model_validate(wf._json_object(result.output))
    except (ValueError, TypeError) as exc:
        return _planner_failure(
            state,
            baseline=baseline,
            target=target,
            attempt=attempt,
            kind="contract",
            error=f"Planner returned an invalid Task Envelope: {exc}",
            output_tail=result.output,
        )
    if task.requirement_ids != [target.id]:
        return _planner_failure(
            state,
            baseline=baseline,
            target=target,
            attempt=attempt,
            kind="contract",
            error=(
                "Planner drifted from deterministic target: "
                f"expected requirement_ids=[{target.id!r}], got {task.requirement_ids!r}"
            ),
            output_tail=result.output,
        )

    success_baseline = dict(baseline)
    success_baseline["planner_control"] = {
        "target_requirement_id": target.id,
        "attempts": 0,
        "last_error": None,
        "last_failure_kind": None,
    }
    next_state: WorkflowState = {
        **state,
        "baseline": success_baseline,
        "task": task.model_dump(mode="json"),
        "iteration": iteration,
        "risk_flags": task.risk_flags,
        "approved_risk_flags": [],
        "tdd_baseline_result": None,
        "tdd_red_result": None,
        "tdd_red_attempts": 0,
        "status": "planned",
    }
    store.write_json(state["run_id"], task.id, "task.json", next_state["task"])
    snapshot = baseline.get("repo_scout") or {}
    store.append_event(
        state["run_id"],
        "planned",
        {
            "task_id": task.id,
            "target_requirement_id": target.id,
            "planner_attempt": attempt,
            "change_kind": task.change_kind,
            "tdd_mode": task.tdd.mode,
            "scout_base_commit": snapshot.get("base_commit"),
            "context_budget_status": (
                result.context.get("budget_status") if result.context else None
            ),
        },
    )
    return next_state


def targeted_plan(state: WorkflowState) -> WorkflowState:
    """Select one objective gap and produce a bounded Task Envelope for only that target."""
    cfg = load_config(state["config_path"])
    requirements = wf._requirements(state)
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
    previous_control = baseline.get("planner_control")
    previous_target = (
        previous_control.get("target_requirement_id")
        if isinstance(previous_control, dict)
        else None
    )
    if previous_target != (target.id if target else None):
        store.write_json(
            state["run_id"],
            "run",
            f"target-selection-{iteration:04d}.json",
            evidence,
        )
        store.append_event(state["run_id"], "target_selected", evidence)

    if target is None:
        baseline.pop("planner_control", None)
        return {
            **prepared,
            "baseline": baseline,
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

    control = _planner_control(baseline, target.id)
    baseline["planner_control"] = control
    prepared = {**prepared, "baseline": baseline}
    return _invoke_target_planner(
        prepared,
        baseline=baseline,
        target=target,
        control=control,
    )


def route_after_targeted_plan(
    state: WorkflowState,
) -> Literal["prepare", "retry", "replan", "human", "end"]:
    status = state.get("status")
    if status == "target_converged":
        return "end"
    if status in {"iteration_budget_exhausted", "planner_human_required"}:
        return "human"
    if status == "planner_retry":
        return "retry"
    if status == "planner_replan_required":
        return "replan"
    return "prepare"


def planner_human_gate(state: WorkflowState) -> WorkflowState:
    """Exception-only HITL after bounded automatic Planner correction is exhausted."""
    baseline = dict(state.get("baseline") or {})
    control = baseline.get("planner_control") if isinstance(baseline, dict) else None
    decision = interrupt(
        {
            "kind": "planner_failure_budget",
            "reason": "Bounded automatic Planner correction was exhausted",
            "target": (baseline.get("target_selection") or {}).get("target_requirement_id"),
            "planner_control": control,
            "allowed": ["retry", "stop"],
        }
    )
    action = decision.get("action") if isinstance(decision, dict) else decision
    human_decisions = wf.record_human_decision(
        state,
        kind="planner_failure_budget",
        action=action,
    )
    if action == "retry":
        if isinstance(control, dict):
            control = dict(control)
            control["attempts"] = 0
            baseline["planner_control"] = control
        return {
            **state,
            "human_decisions": human_decisions,
            "baseline": baseline,
            "replan_attempts": 0,
            "status": "planner_human_retry",
        }
    if action == "stop":
        return {
            **state,
            "human_decisions": human_decisions,
            "status": "stopped",
            "message": "Stopped after Planner failure budget was exhausted",
        }
    raise ValueError(f"Unsupported Planner recovery decision: {action}")


def route_after_planner_human(state: WorkflowState) -> Literal["retry", "end"]:
    return "retry" if state.get("status") == "planner_human_retry" else "end"
