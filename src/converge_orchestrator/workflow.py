from __future__ import annotations

import json
from pathlib import Path
from typing import Any
from uuid import uuid4

from langgraph.graph import END, START, StateGraph
from langgraph.types import interrupt

from .compliance import ComplianceEngine
from .config import load_config
from .control import ControlSignals
from .evidence import EvidenceStore
from .git import (
    cleanup_worktree,
    commit_all,
    create_worktree,
    delete_remote_branch,
    diff,
    push,
    update_base,
)
from .github import GitHubAdapter
from .models import (
    ComplianceSnapshot,
    GateResult,
    Requirement,
    RequirementStatus,
    ReviewResult,
    TaskEnvelope,
    WorkflowState,
)
from .opencode import OpenCodeAdapter
from .policy import DecisionKind, can_integrate
from .prompts import builder_prompt, planner_prompt, repair_prompt, reviewer_prompt
from .quality import required_gates_pass, run_quality_gates, run_scope_gate
from .spec import compile_contract, is_read_only, sha256_file, write_contract
from .verification import run_requirement_verifiers


def _json_object(text: str) -> dict[str, Any]:
    stripped = text.strip()
    try:
        payload = json.loads(stripped)
    except json.JSONDecodeError:
        start = stripped.find("{")
        end = stripped.rfind("}")
        if start < 0 or end <= start:
            raise
        payload = json.loads(stripped[start : end + 1])
    if not isinstance(payload, dict):
        raise ValueError("Agent output must be a JSON object")
    return payload


def _evidence(state: WorkflowState) -> EvidenceStore:
    cfg = load_config(state["config_path"])
    return EvidenceStore(cfg.state_dir / "evidence")


def _task_id(state: WorkflowState) -> str:
    task = state.get("task") or {}
    return str(task.get("id") or "no-task")


def _requirements(state: WorkflowState) -> list[Requirement]:
    return [Requirement.model_validate(item) for item in state["requirements"]]


def _load_existing_compliance(
    cfg,
    contract,
) -> ComplianceSnapshot | None:
    path = cfg.state_dir / "compliance.json"
    if not path.is_file():
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        snapshot = ComplianceSnapshot.model_validate(payload)
    except (json.JSONDecodeError, ValueError):
        return None
    if set(snapshot.entries) != {item.id for item in contract.requirements}:
        return None
    return snapshot


def _persist_compliance(state: WorkflowState, compliance: ComplianceSnapshot) -> None:
    cfg = load_config(state["config_path"])
    cfg.state_dir.mkdir(parents=True, exist_ok=True)
    (cfg.state_dir / "compliance.json").write_text(
        compliance.model_dump_json(indent=2) + "\n",
        encoding="utf-8",
    )


def bootstrap(state: WorkflowState) -> WorkflowState:
    cfg = load_config(state["config_path"])
    cfg.state_dir.mkdir(parents=True, exist_ok=True)
    cfg.worktree_dir.mkdir(parents=True, exist_ok=True)
    if cfg.require_spec_read_only and not is_read_only(cfg.requirements_path):
        raise RuntimeError("Architecture requirements must be OS-level read-only")
    contract = compile_contract(cfg.requirements_path)
    hash_path = cfg.state_dir / "requirements.sha256"
    if hash_path.exists():
        expected = hash_path.read_text(encoding="utf-8").strip()
        if expected != contract.source.sha256:
            raise RuntimeError(
                "Architecture specification hash differs from pinned project intent. "
                "Start an explicit new project/baseline instead of silently accepting drift."
            )
    else:
        hash_path.write_text(contract.source.sha256 + "\n", encoding="utf-8")
    write_contract(cfg.state_dir / "contract.json", contract)
    compliance = _load_existing_compliance(cfg, contract) or ComplianceEngine.initial(contract)
    run_id = state.get("run_id") or uuid4().hex
    next_state: WorkflowState = {
        **state,
        "run_id": run_id,
        "requirements_hash": contract.source.sha256,
        "requirements": [item.model_dump(mode="json") for item in contract.requirements],
        "compliance": compliance.model_dump(mode="json"),
        "iteration": state.get("iteration", 0),
        "repair_attempts": state.get("repair_attempts", 0),
        "replan_attempts": state.get("replan_attempts", 0),
        "risk_flags": state.get("risk_flags", []),
        "approved_risk_flags": state.get("approved_risk_flags", []),
        "status": "bootstrapped",
    }
    _persist_compliance(next_state, compliance)
    store = _evidence(next_state)
    store.append_event(run_id, "bootstrapped", {"sha256": contract.source.sha256})
    return next_state


def verify_spec(state: WorkflowState) -> WorkflowState:
    cfg = load_config(state["config_path"])
    current = sha256_file(cfg.requirements_path)
    if current != state["requirements_hash"]:
        return {
            **state,
            "status": "spec_changed",
            "message": "Immutable architecture specification changed; run stopped.",
        }
    return state


def _safe_point(state: WorkflowState, name: str) -> WorkflowState:
    cfg = load_config(state["config_path"])
    signals = ControlSignals(cfg.state_dir)
    if not signals.pause_requested(state["run_id"]):
        return state
    decision = interrupt(
        {
            "kind": "controlled_pause",
            "run_id": state["run_id"],
            "safe_point": name,
            "task": state.get("task"),
            "status": state.get("status"),
            "allowed": ["resume", "stop"],
        }
    )
    action = decision.get("action") if isinstance(decision, dict) else decision
    signals.clear_pause(state["run_id"])
    if action == "stop":
        return {**state, "status": "stopped", "message": "Stopped at controlled pause"}
    if action != "resume":
        raise ValueError(f"Unsupported pause decision: {action}")
    return {**state, "status": f"resumed_at_{name}"}


def pause_before_plan(state: WorkflowState) -> WorkflowState:
    return _safe_point(state, "before_plan")


def pause_before_build(state: WorkflowState) -> WorkflowState:
    return _safe_point(state, "before_build")


def pause_before_repair(state: WorkflowState) -> WorkflowState:
    return _safe_point(state, "before_repair")


def pause_before_integrate(state: WorkflowState) -> WorkflowState:
    return _safe_point(state, "before_integrate")


def pause_before_pr(state: WorkflowState) -> WorkflowState:
    return _safe_point(state, "before_pr")


def pause_before_merge(state: WorkflowState) -> WorkflowState:
    return _safe_point(state, "before_merge")


def route_after_pause(state: WorkflowState) -> str:
    return "end" if state.get("status") == "stopped" else "continue"


def plan(state: WorkflowState) -> WorkflowState:
    cfg = load_config(state["config_path"])
    update_base(cfg.repo_path, cfg.base_branch)
    requirements = _requirements(state)
    result = OpenCodeAdapter(cfg).invoke(
        "planner",
        planner_prompt(requirements, state["iteration"] + 1),
        cfg.repo_path,
    )
    if not result.ok:
        raise RuntimeError(f"Planner failed: {result.output}")
    task = TaskEnvelope.model_validate(_json_object(result.output))
    known_ids = {item.id for item in requirements}
    unknown_ids = set(task.requirement_ids) - known_ids
    if unknown_ids:
        raise ValueError(f"Planner returned unknown requirement IDs: {sorted(unknown_ids)}")
    next_state: WorkflowState = {
        **state,
        "task": task.model_dump(mode="json"),
        "iteration": state["iteration"] + 1,
        "risk_flags": task.risk_flags,
        "approved_risk_flags": [],
        "status": "planned",
    }
    store = _evidence(next_state)
    store.write_json(next_state["run_id"], task.id, "task.json", next_state["task"])
    store.append_event(next_state["run_id"], "planned", {"task_id": task.id})
    return next_state


def prepare_worktree(state: WorkflowState) -> WorkflowState:
    cfg = load_config(state["config_path"])
    task = TaskEnvelope.model_validate(state["task"])
    worktree, branch = create_worktree(
        cfg.repo_path,
        cfg.worktree_dir,
        task.id,
        cfg.base_branch,
        cfg.branch_prefix,
    )
    return {
        **state,
        "worktree": str(worktree),
        "branch": branch,
        "status": "worktree_ready",
    }


def build(state: WorkflowState) -> WorkflowState:
    cfg = load_config(state["config_path"])
    task = TaskEnvelope.model_validate(state["task"])
    result = OpenCodeAdapter(cfg).invoke(
        "builder",
        builder_prompt(task, _requirements(state)),
        Path(state["worktree"]),
    )
    return {
        **state,
        "message": result.output,
        "status": "built" if result.ok else "builder_failed",
    }


def quality(state: WorkflowState) -> WorkflowState:
    cfg = load_config(state["config_path"])
    task = TaskEnvelope.model_validate(state["task"])
    worktree = Path(state["worktree"])
    results = [run_scope_gate(cfg, worktree, task), *run_quality_gates(cfg, worktree)]
    payload = [item.model_dump(mode="json") for item in results]
    store = _evidence(state)
    store.write_json(state["run_id"], task.id, "quality.json", payload)
    return {**state, "quality_results": payload, "status": "quality_checked"}


def review(state: WorkflowState) -> WorkflowState:
    cfg = load_config(state["config_path"])
    task = TaskEnvelope.model_validate(state["task"])
    requirements = _requirements(state)
    worktree = Path(state["worktree"])
    patch = diff(worktree, cfg.base_branch)
    result = OpenCodeAdapter(cfg).invoke(
        "reviewer",
        reviewer_prompt(task, patch, requirements),
        worktree,
    )
    if result.ok:
        review_result = ReviewResult.model_validate(_json_object(result.output))
    else:
        review_result = ReviewResult(
            verdict="reject",
            findings=[
                {
                    "severity": "major",
                    "reason": result.output,
                    "required_fix": "Reviewer execution must succeed",
                }
            ],
        )
    store = _evidence(state)
    store.write_text(state["run_id"], task.id, "diff.patch", patch)
    store.write_json(
        state["run_id"],
        task.id,
        "review.json",
        review_result.model_dump(mode="json"),
    )
    return {
        **state,
        "review_result": review_result.model_dump(mode="json"),
        "status": "reviewed",
    }


def _integration_decision(state: WorkflowState):
    cfg = load_config(state["config_path"])
    gates = [GateResult.model_validate(item) for item in state.get("quality_results", [])]
    review_result = ReviewResult.model_validate(state["review_result"])
    compliance = ComplianceSnapshot.model_validate(state["compliance"])
    approved = set(state.get("approved_risk_flags", []))
    active_risks = [flag for flag in state.get("risk_flags", []) if flag not in approved]
    return can_integrate(
        expected_spec_hash=state["requirements_hash"],
        current_spec_hash=sha256_file(cfg.requirements_path),
        gates=gates,
        review=review_result,
        compliance=compliance,
        risk_flags=active_risks,
    )


def route_after_review(state: WorkflowState) -> str:
    cfg = load_config(state["config_path"])
    decision = _integration_decision(state)
    if decision.kind == DecisionKind.ALLOW:
        return "integrate"
    if decision.reason == "SPEC_CHANGED":
        return "spec_stop"
    if decision.kind == DecisionKind.INTERRUPT:
        return "human"
    if state.get("repair_attempts", 0) < cfg.max_repair_attempts:
        return "repair"
    if state.get("replan_attempts", 0) < cfg.max_replans:
        return "replan"
    return "human"


def repair(state: WorkflowState) -> WorkflowState:
    cfg = load_config(state["config_path"])
    task = TaskEnvelope.model_validate(state["task"])
    result = OpenCodeAdapter(cfg).invoke(
        "builder",
        repair_prompt(
            task,
            _requirements(state),
            state.get("quality_results", []),
            state.get("review_result"),
        ),
        Path(state["worktree"]),
    )
    return {
        **state,
        "repair_attempts": state.get("repair_attempts", 0) + 1,
        "message": result.output,
        "status": "repaired" if result.ok else "repair_failed",
    }


def _discard_current_workspace(state: WorkflowState) -> None:
    cfg = load_config(state["config_path"])
    branch = state.get("branch")
    worktree = state.get("worktree")
    if state.get("pr") and cfg.github_repo:
        GitHubAdapter(cfg).close_pull_request(int(state["pr"]["number"]))
    if branch:
        delete_remote_branch(cfg.repo_path, branch)
    if branch and worktree:
        cleanup_worktree(cfg.repo_path, Path(worktree), branch)


def replan(state: WorkflowState) -> WorkflowState:
    _discard_current_workspace(state)
    store = _evidence(state)
    store.append_event(
        state["run_id"],
        "replan",
        {"task_id": _task_id(state), "attempt": state.get("replan_attempts", 0) + 1},
    )
    return {
        **state,
        "task": None,
        "worktree": None,
        "branch": None,
        "quality_results": [],
        "review_result": None,
        "commit_sha": None,
        "pr": None,
        "ci": None,
        "repair_attempts": 0,
        "replan_attempts": state.get("replan_attempts", 0) + 1,
        "status": "replanned",
    }


def human_gate(state: WorkflowState) -> WorkflowState:
    decision = interrupt(
        {
            "kind": "human_policy",
            "run_id": state["run_id"],
            "task": state.get("task"),
            "risk_flags": state.get("risk_flags", []),
            "status": state.get("status"),
            "allowed": ["approve", "edit", "reject", "retry"],
        }
    )
    if not isinstance(decision, dict):
        raise ValueError("Human decision must be an object")
    action = decision.get("action")
    if action == "reject":
        _discard_current_workspace(state)
        return {**state, "status": "rejected", "message": "Rejected by human policy"}
    if action == "retry":
        return {**state, "repair_attempts": 0, "status": "human_retry"}
    if action == "edit":
        edited_task = TaskEnvelope.model_validate(decision.get("task"))
        known_ids = {item.id for item in _requirements(state)}
        unknown = set(edited_task.requirement_ids) - known_ids
        if unknown:
            raise ValueError(f"Edited task has unknown requirement IDs: {sorted(unknown)}")
        _discard_current_workspace(state)
        return {
            **state,
            "task": edited_task.model_dump(mode="json"),
            "worktree": None,
            "branch": None,
            "repair_attempts": 0,
            "quality_results": [],
            "review_result": None,
            "status": "human_edited",
        }
    if action == "approve":
        current = _integration_decision(state)
        if current.kind != DecisionKind.INTERRUPT:
            raise ValueError("Human approval cannot override deterministic gate or CI failures")
        approved = sorted(set(state.get("approved_risk_flags", [])) | set(state["risk_flags"]))
        return {
            **state,
            "approved_risk_flags": approved,
            "status": "human_approved",
        }
    raise ValueError(f"Unsupported human decision: {action}")


def route_after_human(state: WorkflowState) -> str:
    status = state.get("status")
    if status == "rejected":
        return "end"
    if status == "human_retry":
        return "repair"
    if status == "human_edited":
        return "prepare"
    if status == "human_approved":
        return "integrate"
    return "end"


def integrate(state: WorkflowState) -> WorkflowState:
    cfg = load_config(state["config_path"])
    task = TaskEnvelope.model_validate(state["task"])
    worktree = Path(state["worktree"])
    commit_sha = commit_all(worktree, f"converge: {task.title}")
    if commit_sha is None:
        return {**state, "status": "no_changes", "message": "Builder produced no changes"}
    push(worktree, state["branch"])
    return {**state, "commit_sha": commit_sha, "status": "pushed"}


def create_pr(state: WorkflowState) -> WorkflowState:
    cfg = load_config(state["config_path"])
    if not cfg.github_repo:
        return {**state, "status": "local_complete"}
    task = TaskEnvelope.model_validate(state["task"])
    pr = GitHubAdapter(cfg).create_pull_request(
        branch=state["branch"],
        title=f"converge: {task.title}",
        body=(
            f"Automated bounded convergence task `{task.id}`.\n\n"
            f"Requirements: {', '.join(task.requirement_ids)}\n"
            f"Objective: {task.objective}\n"
        ),
    )
    store = _evidence(state)
    store.write_json(state["run_id"], task.id, "pr.json", pr.model_dump(mode="json"))
    return {**state, "pr": pr.model_dump(mode="json"), "status": "pr_created"}


def wait_ci(state: WorkflowState) -> WorkflowState:
    cfg = load_config(state["config_path"])
    if not cfg.github_repo or not state.get("pr"):
        return {**state, "status": "ci_skipped"}
    result = GitHubAdapter(cfg).wait_for_ci(state["pr"]["head_sha"])
    store = _evidence(state)
    store.write_json(state["run_id"], _task_id(state), "ci.json", result.model_dump(mode="json"))
    return {**state, "ci": result.model_dump(mode="json"), "status": f"ci_{result.status}"}


def route_after_ci(state: WorkflowState) -> str:
    cfg = load_config(state["config_path"])
    ci = state.get("ci") or {}
    if not cfg.github_repo:
        return "end"
    if ci.get("status") == "pass":
        return "merge" if cfg.auto_merge else "end"
    if state.get("repair_attempts", 0) < cfg.max_repair_attempts:
        return "repair"
    if state.get("replan_attempts", 0) < cfg.max_replans:
        return "replan"
    return "human"


def merge_pr(state: WorkflowState) -> WorkflowState:
    cfg = load_config(state["config_path"])
    if not cfg.github_repo or not state.get("pr"):
        return {**state, "status": "merge_skipped"}
    task = TaskEnvelope.model_validate(state["task"])
    GitHubAdapter(cfg).merge_pull_request(int(state["pr"]["number"]))
    compliance = ComplianceSnapshot.model_validate(state["compliance"])
    compliance = ComplianceEngine.mark_remote_verified(
        compliance,
        task.requirement_ids,
        [f"github-pr:{state['pr']['number']}", f"commit:{state['pr']['head_sha']}"],
    )
    _persist_compliance(state, compliance)
    store = _evidence(state)
    store.append_event(
        state["run_id"],
        "merged",
        {"task_id": task.id, "pr": state["pr"]["number"]},
    )
    return {
        **state,
        "compliance": compliance.model_dump(mode="json"),
        "status": "merged",
    }


def evaluate_compliance(state: WorkflowState) -> WorkflowState:
    cfg = load_config(state["config_path"])
    update_base(cfg.repo_path, cfg.base_branch)
    contract = compile_contract(cfg.requirements_path)
    verifications = run_requirement_verifiers(cfg, cfg.repo_path, contract.requirements)
    compliance = ComplianceEngine.apply_verifications(
        ComplianceSnapshot.model_validate(state["compliance"]),
        verifications,
    )
    _persist_compliance(state, compliance)
    return {
        **state,
        "compliance": compliance.model_dump(mode="json"),
        "requirement_verifications": [
            item.model_dump(mode="json") for item in verifications
        ],
        "status": "compliance_evaluated",
    }


def route_after_compliance(state: WorkflowState) -> str:
    cfg = load_config(state["config_path"])
    compliance = ComplianceSnapshot.model_validate(state["compliance"])
    requirements = _requirements(state)
    mandatory_ids = {item.id for item in requirements if item.severity == "mandatory"}
    unresolved = [
        requirement_id
        for requirement_id in mandatory_ids
        if compliance.entries[requirement_id].status != RequirementStatus.PASS
    ]
    if not unresolved:
        return "converged"
    if state.get("iteration", 0) >= cfg.max_iterations:
        return "budget"
    return "next"


def mark_converged(state: WorkflowState) -> WorkflowState:
    return {**state, "status": "converged", "message": "All mandatory requirements pass"}


def mark_budget_exhausted(state: WorkflowState) -> WorkflowState:
    return {
        **state,
        "status": "budget_exhausted",
        "message": "Maximum convergence iterations reached",
    }


def mark_spec_changed(state: WorkflowState) -> WorkflowState:
    return {
        **state,
        "status": "spec_changed",
        "message": "Immutable architecture specification changed; run stopped.",
    }


def build_graph(checkpointer=None):
    graph = StateGraph(WorkflowState)
    graph.add_node("bootstrap", bootstrap)
    graph.add_node("verify_spec", verify_spec)
    graph.add_node("pause_before_plan", pause_before_plan)
    graph.add_node("plan", plan)
    graph.add_node("prepare_worktree", prepare_worktree)
    graph.add_node("pause_before_build", pause_before_build)
    graph.add_node("build", build)
    graph.add_node("quality", quality)
    graph.add_node("review", review)
    graph.add_node("pause_before_repair", pause_before_repair)
    graph.add_node("repair", repair)
    graph.add_node("replan", replan)
    graph.add_node("human", human_gate)
    graph.add_node("pause_before_integrate", pause_before_integrate)
    graph.add_node("integrate", integrate)
    graph.add_node("pause_before_pr", pause_before_pr)
    graph.add_node("create_pr", create_pr)
    graph.add_node("wait_ci", wait_ci)
    graph.add_node("pause_before_merge", pause_before_merge)
    graph.add_node("merge", merge_pr)
    graph.add_node("evaluate_compliance", evaluate_compliance)
    graph.add_node("converged", mark_converged)
    graph.add_node("budget_exhausted", mark_budget_exhausted)
    graph.add_node("spec_changed", mark_spec_changed)

    graph.add_edge(START, "bootstrap")
    graph.add_edge("bootstrap", "verify_spec")
    graph.add_conditional_edges(
        "verify_spec",
        lambda state: "stop" if state.get("status") == "spec_changed" else "continue",
        {"stop": "spec_changed", "continue": "pause_before_plan"},
    )
    graph.add_conditional_edges(
        "pause_before_plan",
        route_after_pause,
        {"end": END, "continue": "plan"},
    )
    graph.add_edge("plan", "prepare_worktree")
    graph.add_edge("prepare_worktree", "pause_before_build")
    graph.add_conditional_edges(
        "pause_before_build",
        route_after_pause,
        {"end": END, "continue": "build"},
    )
    graph.add_edge("build", "verify_spec")
    graph.add_conditional_edges(
        "verify_spec",
        lambda state: (
            "stop"
            if state.get("status") == "spec_changed"
            else "continue"
        ),
        {"stop": "spec_changed", "continue": "quality"},
    )
    graph.add_edge("quality", "review")
    graph.add_conditional_edges(
        "review",
        route_after_review,
        {
            "integrate": "pause_before_integrate",
            "spec_stop": "spec_changed",
            "human": "human",
            "repair": "pause_before_repair",
            "replan": "replan",
        },
    )
    graph.add_conditional_edges(
        "pause_before_repair",
        route_after_pause,
        {"end": END, "continue": "repair"},
    )
    graph.add_edge("repair", "quality")
    graph.add_edge("replan", "pause_before_plan")
    graph.add_conditional_edges(
        "human",
        route_after_human,
        {
            "end": END,
            "repair": "pause_before_repair",
            "prepare": "prepare_worktree",
            "integrate": "pause_before_integrate",
        },
    )
    graph.add_conditional_edges(
        "pause_before_integrate",
        route_after_pause,
        {"end": END, "continue": "integrate"},
    )
    graph.add_edge("integrate", "pause_before_pr")
    graph.add_conditional_edges(
        "pause_before_pr",
        route_after_pause,
        {"end": END, "continue": "create_pr"},
    )
    graph.add_edge("create_pr", "wait_ci")
    graph.add_conditional_edges(
        "wait_ci",
        route_after_ci,
        {
            "end": END,
            "repair": "pause_before_repair",
            "replan": "replan",
            "human": "human",
            "merge": "pause_before_merge",
        },
    )
    graph.add_conditional_edges(
        "pause_before_merge",
        route_after_pause,
        {"end": END, "continue": "merge"},
    )
    graph.add_edge("merge", "evaluate_compliance")
    graph.add_conditional_edges(
        "evaluate_compliance",
        route_after_compliance,
        {
            "next": "pause_before_plan",
            "converged": "converged",
            "budget": "budget_exhausted",
        },
    )
    graph.add_edge("converged", END)
    graph.add_edge("budget_exhausted", END)
    graph.add_edge("spec_changed", END)
    return graph.compile(checkpointer=checkpointer)
