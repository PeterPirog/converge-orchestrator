from __future__ import annotations

import json
import re
from pathlib import Path
from uuid import uuid4

from langgraph.graph import END, START, StateGraph
from langgraph.types import interrupt

from .compliance import ComplianceEngine
from .config import load_config
from .evidence import EvidenceStore
from .git import (
    cleanup_worktree,
    commit_all,
    create_worktree,
    delete_remote_branch,
    diff,
    ensure_clean,
    push,
    update_base,
)
from .github import GitHubAdapter
from .models import (
    ComplianceSnapshot,
    GateResult,
    Requirement,
    ReviewResult,
    TaskEnvelope,
    WorkflowState,
)
from .opencode import OpenCodeAdapter
from .policy import DecisionKind, can_integrate
from .prompts import builder_prompt, planner_prompt, repair_prompt, reviewer_prompt
from .quality import required_gates_pass, run_quality_gates, run_scope_gate
from .spec import compile_contract, is_read_only, sha256_file, write_contract


def _json_object(text: str) -> dict:
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        match = re.search(r"\{.*\}", text, re.DOTALL)
        if not match:
            raise ValueError("Agent did not return a JSON object") from None
        return json.loads(match.group(0))


def _evidence(state: WorkflowState) -> EvidenceStore:
    cfg = load_config(state["config_path"])
    return EvidenceStore(cfg.state_dir / "evidence")


def _task_id(state: WorkflowState) -> str:
    task = state.get("task")
    return str(task.get("id")) if task else "run"


def bootstrap(state: WorkflowState) -> WorkflowState:
    cfg = load_config(state["config_path"])
    if not cfg.repo_path.exists() or not (cfg.repo_path / ".git").exists():
        raise RuntimeError(f"Not a git repository: {cfg.repo_path}")
    if not cfg.requirements_path.is_file():
        raise RuntimeError(f"Requirements file not found: {cfg.requirements_path}")
    if cfg.require_spec_read_only and not is_read_only(cfg.requirements_path):
        raise RuntimeError("Requirements file must be read-only before orchestration starts.")
    ensure_clean(cfg.repo_path)
    expected = sha256_file(cfg.requirements_path)
    hash_path = cfg.state_dir / "requirements.sha256"
    if hash_path.exists() and hash_path.read_text().strip() != expected:
        raise RuntimeError(
            "Requirements changed since project initialization; refusing to continue."
        )
    hash_path.write_text(expected, encoding="utf-8")
    contract = compile_contract(cfg.requirements_path)
    write_contract(cfg.state_dir / "contract.json", contract)
    compliance = ComplianceEngine.initial(contract)
    run_id = state.get("run_id") or uuid4().hex
    store = EvidenceStore(cfg.state_dir / "evidence")
    store.append_event(
        run_id,
        "bootstrap",
        {"requirements_sha256": expected, "requirements": len(contract.requirements)},
    )
    return {
        **state,
        "run_id": run_id,
        "requirements_hash": expected,
        "requirements": [item.model_dump(mode="json") for item in contract.requirements],
        "compliance": compliance.model_dump(mode="json"),
        "iteration": state.get("iteration", 0),
        "repair_attempts": 0,
        "replan_attempts": 0,
        "risk_flags": [],
        "status": "bootstrapped",
    }


def guard_spec(state: WorkflowState) -> WorkflowState:
    cfg = load_config(state["config_path"])
    current = sha256_file(cfg.requirements_path)
    if current != state["requirements_hash"]:
        _evidence(state).append_event(
            state["run_id"],
            "spec_changed",
            {"expected": state["requirements_hash"], "actual": current},
        )
        return {
            **state,
            "status": "spec_changed",
            "message": "Immutable architecture specification changed; run stopped.",
        }
    return {**state, "status": "spec_ok"}


def route_after_guard(state: WorkflowState) -> str:
    return "continue" if state.get("status") == "spec_ok" else "end"


def spec_stop(state: WorkflowState) -> WorkflowState:
    return {
        **state,
        "status": "spec_changed",
        "message": "Immutable architecture specification changed; run stopped.",
    }


def plan(state: WorkflowState) -> WorkflowState:
    cfg = load_config(state["config_path"])
    update_base(cfg.repo_path, cfg.base_branch)
    requirements = [Requirement.model_validate(item) for item in state["requirements"]]
    result = OpenCodeAdapter(cfg).invoke(
        "planner",
        planner_prompt(requirements, state["iteration"] + 1),
        cfg.repo_path,
    )
    if not result.ok:
        raise RuntimeError(f"Planner failed: {result.output}")
    task = TaskEnvelope.model_validate(_json_object(result.output))
    next_state = {
        **state,
        "task": task.model_dump(mode="json"),
        "iteration": state["iteration"] + 1,
        "risk_flags": task.risk_flags,
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
        builder_prompt(task, cfg.requirements_path),
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
    requirements = [Requirement.model_validate(item) for item in state["requirements"]]
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


def route_after_review(state: WorkflowState) -> str:
    cfg = load_config(state["config_path"])
    gates = [GateResult.model_validate(item) for item in state.get("quality_results", [])]
    review_result = ReviewResult.model_validate(state["review_result"])
    compliance = ComplianceSnapshot.model_validate(state["compliance"])
    decision = can_integrate(
        expected_spec_hash=state["requirements_hash"],
        current_spec_hash=sha256_file(cfg.requirements_path),
        gates=gates,
        review=review_result,
        compliance=compliance,
        risk_flags=state.get("risk_flags", []),
    )
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


def replan(state: WorkflowState) -> WorkflowState:
    cfg = load_config(state["config_path"])
    branch = state.get("branch")
    worktree = state.get("worktree")
    if state.get("pr") and cfg.github_repo:
        GitHubAdapter(cfg).close_pull_request(int(state["pr"]["number"]))
    if branch:
        delete_remote_branch(cfg.repo_path, branch)
    if branch and worktree:
        cleanup_worktree(cfg.repo_path, Path(worktree), branch)
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
        "status": "replanning",
    }


def human_gate(state: WorkflowState) -> WorkflowState:
    decision = interrupt(
        {
            "reason": "Autonomous repair/replan budget or risk policy requires intervention",
            "task": state.get("task"),
            "quality": state.get("quality_results"),
            "review": state.get("review_result"),
            "risk_flags": state.get("risk_flags"),
            "allowed": ["retry", "stop"],
        }
    )
    if decision == "retry":
        return {
            **state,
            "repair_attempts": 0,
            "replan_attempts": 0,
            "status": "human_retry",
        }
    return {**state, "status": "stopped", "message": "Stopped by human operator"}


def route_after_human(state: WorkflowState) -> str:
    return "plan" if state["status"] == "human_retry" else "end"


def integrate(state: WorkflowState) -> WorkflowState:
    cfg = load_config(state["config_path"])
    if sha256_file(cfg.requirements_path) != state["requirements_hash"]:
        return spec_stop(state)
    task = TaskEnvelope.model_validate(state["task"])
    worktree = Path(state["worktree"])
    commit = commit_all(worktree, f"feat: {task.title}")
    if commit:
        push(worktree, state["branch"])
    store = _evidence(state)
    store.append_event(
        state["run_id"],
        "pushed",
        {"task_id": task.id, "branch": state["branch"], "commit_sha": commit},
    )
    compliance = ComplianceSnapshot.model_validate(state["compliance"])
    gates = [GateResult.model_validate(item) for item in state["quality_results"]]
    if required_gates_pass(gates):
        compliance = ComplianceEngine.mark_local_pass(
            compliance,
            task.requirement_ids,
            [f"local-gates:{state['run_id']}:{task.id}", "independent-review:pass"],
        )
    return {
        **state,
        "commit_sha": commit,
        "compliance": compliance.model_dump(mode="json"),
        "status": "pushed" if commit else "no_changes",
        "message": commit or "No changes produced",
    }


def route_after_integrate(state: WorkflowState) -> str:
    cfg = load_config(state["config_path"])
    if state.get("status") == "spec_changed" or not state.get("commit_sha"):
        return "end"
    return "pr" if cfg.github_repo else "end"


def _pull_request_body(state: WorkflowState) -> str:
    task = TaskEnvelope.model_validate(state["task"])
    quality_lines = [
        f"- {item['name']}: {'PASS' if item['ok'] else 'FAIL'}"
        for item in state.get("quality_results", [])
    ]
    return "\n".join(
        [
            "## Objective",
            f"{', '.join(task.requirement_ids)} — {task.objective}",
            "",
            "## Verification",
            *quality_lines,
            "- independent review: PASS",
            "",
            "## Risk",
            task.risk,
            "",
            "## Converge metadata",
            f"- run: {state['run_id']}",
            f"- task: {task.id}",
            f"- requirements_sha256: {state['requirements_hash']}",
        ]
    )


def create_pr(state: WorkflowState) -> WorkflowState:
    cfg = load_config(state["config_path"])
    task = TaskEnvelope.model_validate(state["task"])
    adapter = GitHubAdapter(cfg)
    existing = state.get("pr")
    if existing:
        pr = adapter.get_pull_request(int(existing["number"]))
    else:
        pr = adapter.create_pull_request(
            head=state["branch"],
            base=cfg.base_branch,
            title=f"[Converge] {task.title}",
            body=_pull_request_body(state),
        )
    store = _evidence(state)
    store.write_json(state["run_id"], task.id, "pr.json", pr.model_dump(mode="json"))
    return {**state, "pr": pr.model_dump(mode="json"), "status": "pr_open"}


def ci_gate(state: WorkflowState) -> WorkflowState:
    cfg = load_config(state["config_path"])
    task = TaskEnvelope.model_validate(state["task"])
    pr = state["pr"]
    result = GitHubAdapter(cfg).wait_for_ci(pr["head_sha"])
    store = _evidence(state)
    store.write_json(state["run_id"], task.id, "ci.json", result.model_dump(mode="json"))
    return {**state, "ci": result.model_dump(mode="json"), "status": f"ci_{result.status}"}


def route_after_ci(state: WorkflowState) -> str:
    if state["ci"]["status"] == "pass":
        cfg = load_config(state["config_path"])
        return "merge" if cfg.auto_merge else "end"
    cfg = load_config(state["config_path"])
    if state.get("repair_attempts", 0) < cfg.max_repair_attempts:
        return "repair"
    if state.get("replan_attempts", 0) < cfg.max_replans:
        return "replan"
    return "human"


def merge_pr(state: WorkflowState) -> WorkflowState:
    cfg = load_config(state["config_path"])
    task = TaskEnvelope.model_validate(state["task"])
    merged_sha = GitHubAdapter(cfg).merge(int(state["pr"]["number"]))
    compliance = ComplianceSnapshot.model_validate(state["compliance"])
    compliance = ComplianceEngine.mark_remote_verified(
        compliance,
        task.requirement_ids,
        [f"github-pr:{state['pr']['url']}", f"merged-sha:{merged_sha}"],
    )
    store = _evidence(state)
    store.append_event(
        state["run_id"],
        "merged",
        {"task_id": task.id, "pr": state["pr"]["url"], "sha": merged_sha},
    )
    if state.get("branch"):
        delete_remote_branch(cfg.repo_path, state["branch"])
    if state.get("branch") and state.get("worktree"):
        cleanup_worktree(
            cfg.repo_path,
            Path(state["worktree"]),
            state["branch"],
        )
    return {
        **state,
        "compliance": compliance.model_dump(mode="json"),
        "status": "merged",
        "message": merged_sha,
    }


def build_graph(checkpointer=None):
    graph = StateGraph(WorkflowState)
    nodes = [
        ("bootstrap", bootstrap),
        ("guard_plan", guard_spec),
        ("guard_quality", guard_spec),
        ("spec_stop", spec_stop),
        ("plan", plan),
        ("prepare_worktree", prepare_worktree),
        ("build", build),
        ("quality", quality),
        ("review", review),
        ("repair", repair),
        ("replan", replan),
        ("human", human_gate),
        ("integrate", integrate),
        ("pr", create_pr),
        ("ci", ci_gate),
        ("merge", merge_pr),
    ]
    for name, node in nodes:
        graph.add_node(name, node)
    graph.add_edge(START, "bootstrap")
    graph.add_edge("bootstrap", "guard_plan")
    graph.add_conditional_edges(
        "guard_plan",
        route_after_guard,
        {"continue": "plan", "end": END},
    )
    graph.add_edge("plan", "prepare_worktree")
    graph.add_edge("prepare_worktree", "build")
    graph.add_edge("build", "guard_quality")
    graph.add_conditional_edges(
        "guard_quality",
        route_after_guard,
        {"continue": "quality", "end": END},
    )
    graph.add_edge("quality", "review")
    graph.add_conditional_edges(
        "review",
        route_after_review,
        {
            "integrate": "integrate",
            "repair": "repair",
            "replan": "replan",
            "human": "human",
            "spec_stop": "spec_stop",
        },
    )
    graph.add_edge("spec_stop", END)
    graph.add_edge("repair", "guard_quality")
    graph.add_edge("replan", "guard_plan")
    graph.add_conditional_edges(
        "human",
        route_after_human,
        {"plan": "guard_plan", "end": END},
    )
    graph.add_conditional_edges(
        "integrate",
        route_after_integrate,
        {"pr": "pr", "end": END},
    )
    graph.add_edge("pr", "ci")
    graph.add_conditional_edges(
        "ci",
        route_after_ci,
        {
            "merge": "merge",
            "repair": "repair",
            "replan": "replan",
            "human": "human",
            "end": END,
        },
    )
    graph.add_edge("merge", END)
    return graph.compile(checkpointer=checkpointer)
