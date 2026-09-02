from __future__ import annotations

import json
import re
from pathlib import Path

from langgraph.graph import END, START, StateGraph
from langgraph.types import interrupt

from .config import load_config
from .git import commit_all, create_worktree, diff, ensure_clean, push, update_base
from .models import GateResult, Requirement, TaskEnvelope, WorkflowState
from .opencode import OpenCodeAdapter
from .prompts import builder_prompt, planner_prompt, repair_prompt, reviewer_prompt
from .quality import required_gates_pass, run_quality_gates
from .spec import compile_contract, sha256_file, write_contract


def _json_object(text: str) -> dict:
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        match = re.search(r"\{.*\}", text, re.DOTALL)
        if not match:
            raise ValueError("Agent did not return a JSON object") from None
        return json.loads(match.group(0))


def bootstrap(state: WorkflowState) -> WorkflowState:
    cfg = load_config(state["config_path"])
    if not cfg.repo_path.exists() or not (cfg.repo_path / ".git").exists():
        raise RuntimeError(f"Not a git repository: {cfg.repo_path}")
    if not cfg.requirements_path.is_file():
        raise RuntimeError(f"Requirements file not found: {cfg.requirements_path}")
    ensure_clean(cfg.repo_path)
    expected = sha256_file(cfg.requirements_path)
    hash_path = cfg.state_dir / "requirements.sha256"
    if hash_path.exists() and hash_path.read_text().strip() != expected:
        raise RuntimeError("Requirements changed since project initialization; refusing to continue.")
    hash_path.write_text(expected, encoding="utf-8")
    requirements = compile_contract(cfg.requirements_path)
    write_contract(cfg.state_dir / "contract.json", requirements)
    return {**state, "requirements_hash": expected, "requirements": [r.model_dump(mode="json") for r in requirements], "iteration": state.get("iteration", 0), "repair_attempts": 0, "replan_attempts": 0, "status": "bootstrapped"}


def guard_spec(state: WorkflowState) -> WorkflowState:
    cfg = load_config(state["config_path"])
    if sha256_file(cfg.requirements_path) != state["requirements_hash"]:
        raise RuntimeError("Immutable architecture specification changed during the run.")
    return state


def plan(state: WorkflowState) -> WorkflowState:
    cfg = load_config(state["config_path"])
    update_base(cfg.repo_path, cfg.base_branch)
    reqs = [Requirement.model_validate(item) for item in state["requirements"]]
    result = OpenCodeAdapter(cfg).invoke("planner", planner_prompt(reqs, state["iteration"] + 1), cfg.repo_path)
    if not result.ok:
        raise RuntimeError(f"Planner failed: {result.output}")
    task = TaskEnvelope.model_validate(_json_object(result.output))
    return {**state, "task": task.model_dump(mode="json"), "iteration": state["iteration"] + 1, "status": "planned"}


def prepare_worktree(state: WorkflowState) -> WorkflowState:
    cfg = load_config(state["config_path"])
    task = TaskEnvelope.model_validate(state["task"])
    worktree, branch = create_worktree(cfg.repo_path, cfg.worktree_dir, task.id, cfg.base_branch)
    return {**state, "worktree": str(worktree), "branch": branch, "status": "worktree_ready"}


def build(state: WorkflowState) -> WorkflowState:
    cfg = load_config(state["config_path"])
    task = TaskEnvelope.model_validate(state["task"])
    result = OpenCodeAdapter(cfg).invoke("builder", builder_prompt(task, cfg.requirements_path), Path(state["worktree"]))
    return {**state, "message": result.output, "status": "built" if result.ok else "builder_failed"}


def quality(state: WorkflowState) -> WorkflowState:
    cfg = load_config(state["config_path"])
    results = run_quality_gates(cfg, Path(state["worktree"]))
    return {**state, "quality_results": [r.model_dump() for r in results], "status": "quality_checked"}


def review(state: WorkflowState) -> WorkflowState:
    cfg = load_config(state["config_path"])
    task = TaskEnvelope.model_validate(state["task"])
    reqs = [Requirement.model_validate(item) for item in state["requirements"]]
    result = OpenCodeAdapter(cfg).invoke("reviewer", reviewer_prompt(task, diff(Path(state["worktree"]), cfg.base_branch), reqs), Path(state["worktree"]))
    payload = _json_object(result.output) if result.ok else {"verdict": "reject", "findings": [{"severity": "major", "reason": result.output, "required_fix": "Reviewer execution failed"}]}
    return {**state, "review_result": payload, "status": "reviewed"}


def route_after_review(state: WorkflowState) -> str:
    quality_ok = required_gates_pass([GateResult.model_validate(x) for x in state.get("quality_results", [])])
    review_ok = (state.get("review_result") or {}).get("verdict") == "approve"
    if quality_ok and review_ok:
        return "integrate"
    cfg = load_config(state["config_path"])
    if state.get("repair_attempts", 0) < cfg.max_repair_attempts:
        return "repair"
    if state.get("replan_attempts", 0) < cfg.max_replans:
        return "replan"
    return "human"


def repair(state: WorkflowState) -> WorkflowState:
    cfg = load_config(state["config_path"])
    task = TaskEnvelope.model_validate(state["task"])
    result = OpenCodeAdapter(cfg).invoke("builder", repair_prompt(task, state.get("quality_results", []), state.get("review_result")), Path(state["worktree"]))
    return {**state, "repair_attempts": state.get("repair_attempts", 0) + 1, "message": result.output, "status": "repaired" if result.ok else "repair_failed"}


def replan(state: WorkflowState) -> WorkflowState:
    return {**state, "task": None, "worktree": None, "branch": None, "repair_attempts": 0, "replan_attempts": state.get("replan_attempts", 0) + 1, "status": "replanning"}


def human_gate(state: WorkflowState) -> WorkflowState:
    decision = interrupt({"reason": "Autonomous repair/replan budget exhausted", "task": state.get("task"), "quality": state.get("quality_results"), "review": state.get("review_result"), "allowed": ["retry", "stop"]})
    if decision == "retry":
        return {**state, "repair_attempts": 0, "replan_attempts": 0, "status": "human_retry"}
    return {**state, "status": "stopped", "message": "Stopped by human operator"}


def route_after_human(state: WorkflowState) -> str:
    return "plan" if state["status"] == "human_retry" else "end"


def integrate(state: WorkflowState) -> WorkflowState:
    cfg = load_config(state["config_path"])
    task = TaskEnvelope.model_validate(state["task"])
    worktree = Path(state["worktree"])
    commit = commit_all(worktree, f"feat: {task.title}")
    if commit:
        push(worktree, state["branch"])
    return {**state, "status": "pushed", "message": commit or "No changes produced"}


def build_graph(checkpointer=None):
    graph = StateGraph(WorkflowState)
    for name, node in [("bootstrap", bootstrap), ("guard_spec", guard_spec), ("plan", plan), ("prepare_worktree", prepare_worktree), ("build", build), ("quality", quality), ("review", review), ("repair", repair), ("replan", replan), ("human", human_gate), ("integrate", integrate)]:
        graph.add_node(name, node)
    graph.add_edge(START, "bootstrap")
    graph.add_edge("bootstrap", "guard_spec")
    graph.add_edge("guard_spec", "plan")
    graph.add_edge("plan", "prepare_worktree")
    graph.add_edge("prepare_worktree", "build")
    graph.add_edge("build", "quality")
    graph.add_edge("quality", "review")
    graph.add_conditional_edges("review", route_after_review, {"integrate": "integrate", "repair": "repair", "replan": "replan", "human": "human"})
    graph.add_edge("repair", "quality")
    graph.add_edge("replan", "guard_spec")
    graph.add_conditional_edges("human", route_after_human, {"plan": "guard_spec", "end": END})
    graph.add_edge("integrate", END)
    return graph.compile(checkpointer=checkpointer)
