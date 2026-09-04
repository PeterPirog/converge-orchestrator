from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Literal

from langgraph.graph import END, START, StateGraph
from langgraph.types import interrupt
from pydantic import BaseModel, Field

from . import workflow as wf
from .config import load_config
from .context import AdvisorySection, PromptEnvelope, build_working_memory
from .git import update_base
from .models import GateResult, Requirement, TaskEnvelope, WorkflowState
from .opencode import OpenCodeAdapter
from .prompts import builder_prompt, contract_excerpt, tdd_red_prompt
from .quality import run_quality_gates, run_scope_gate
from .tdd import requires_tdd, run_tdd_baseline, run_tdd_green, run_tdd_red


class RepoScoutPayload(BaseModel):
    """Bounded semantic map produced by the read-only repository scout."""

    summary: str = ""
    stacks: list[str] = Field(default_factory=list)
    key_paths: list[str] = Field(default_factory=list)
    test_paths: list[str] = Field(default_factory=list)
    architecture_notes: list[str] = Field(default_factory=list)
    risk_notes: list[str] = Field(default_factory=list)
    requirement_hints: dict[str, list[str]] = Field(default_factory=dict)
    uncertainties: list[str] = Field(default_factory=list)


class RepoScoutSnapshot(RepoScoutPayload):
    base_branch: str
    base_commit: str
    source: Literal["agent", "fallback"]
    warnings: list[str] = Field(default_factory=list)


def _bounded_text(value: str, limit: int = 4000) -> str:
    return value.strip()[:limit]


def _bounded_list(values: list[str], *, items: int = 40, chars: int = 500) -> list[str]:
    output: list[str] = []
    seen: set[str] = set()
    for raw in values:
        value = raw.strip()[:chars]
        if not value or value in seen:
            continue
        output.append(value)
        seen.add(value)
        if len(output) >= items:
            break
    return output


def _normalize_payload(
    payload: RepoScoutPayload,
    requirements: list[Requirement],
) -> tuple[RepoScoutPayload, list[str]]:
    known_ids = {item.id for item in requirements}
    warnings: list[str] = []
    hints: dict[str, list[str]] = {}
    for requirement_id, paths in payload.requirement_hints.items():
        if requirement_id not in known_ids:
            warnings.append(f"ignored unknown requirement hint: {requirement_id}")
            continue
        hints[requirement_id] = _bounded_list(paths, items=12, chars=300)
        if len(hints) >= 40:
            warnings.append("requirement hints truncated to 40 entries")
            break
    normalized = RepoScoutPayload(
        summary=_bounded_text(payload.summary),
        stacks=_bounded_list(payload.stacks, items=20, chars=120),
        key_paths=_bounded_list(payload.key_paths),
        test_paths=_bounded_list(payload.test_paths),
        architecture_notes=_bounded_list(payload.architecture_notes, items=24),
        risk_notes=_bounded_list(payload.risk_notes, items=24),
        requirement_hints=hints,
        uncertainties=_bounded_list(payload.uncertainties, items=24),
    )
    return normalized, warnings


def _scout_prompt(requirements: list[Requirement], base_branch: str, base_commit: str) -> str:
    schema = {
        "summary": "Short current repository map",
        "stacks": ["python"],
        "key_paths": ["src/package/service.py"],
        "test_paths": ["tests/test_service.py"],
        "architecture_notes": ["Domain layer currently imports infrastructure adapter"],
        "risk_notes": ["Public API surface exists under src/package/api.py"],
        "requirement_hints": {"ARCH-001": ["src/package/service.py"]},
        "uncertainties": ["No integration-test fixture found for external provider"],
    }
    return f"""Act as a fast read-only repository scout for an autonomous engineering workflow.
Do not edit files. Build a compact factual map of the repository at the exact base commit below.
Use repository evidence, not guesses. Focus on structure, relevant code/test paths, architectural
boundaries, risky surfaces and where immutable requirements appear to map into code. Do NOT choose
or design the implementation task; Planner owns that decision. Unknown requirement IDs are invalid.
Keep the output bounded and return ONLY JSON matching this shape: {json.dumps(schema)}
Base branch: {base_branch}
Base commit: {base_commit}

IMMUTABLE REQUIREMENTS:
{contract_excerpt(requirements)}
"""


def _fallback_snapshot(
    *,
    base_branch: str,
    base_commit: str,
    warning: str,
) -> RepoScoutSnapshot:
    return RepoScoutSnapshot(
        base_branch=base_branch,
        base_commit=base_commit,
        source="fallback",
        summary=(
            "Repository scout data is unavailable. Planner must inspect the repository directly "
            "before selecting a task."
        ),
        warnings=[_bounded_text(warning, 2000)],
    )


def _gate_from_state(state: WorkflowState, key: str) -> GateResult | None:
    payload = state.get(key)
    return GateResult.model_validate(payload) if payload else None


def scout(state: WorkflowState) -> WorkflowState:
    """Refresh canonical base and produce a bounded read-only repository map."""
    cfg = load_config(state["config_path"])
    base_commit = update_base(cfg.repo_path, cfg.base_branch)
    requirements = wf._requirements(state)
    context_report: dict[str, Any] | None = None

    if "scout" not in cfg.agents:
        snapshot = _fallback_snapshot(
            base_branch=cfg.base_branch,
            base_commit=base_commit,
            warning="agent role 'scout' is not configured",
        )
    else:
        result = OpenCodeAdapter(cfg).invoke(
            "scout",
            _scout_prompt(requirements, cfg.base_branch, base_commit),
            cfg.repo_path,
        )
        context_report = result.context
        if not result.ok:
            snapshot = _fallback_snapshot(
                base_branch=cfg.base_branch,
                base_commit=base_commit,
                warning=f"scout execution failed: {result.output[-1200:]}",
            )
        else:
            try:
                payload = RepoScoutPayload.model_validate(wf._json_object(result.output))
                payload, warnings = _normalize_payload(payload, requirements)
                snapshot = RepoScoutSnapshot(
                    **payload.model_dump(),
                    base_branch=cfg.base_branch,
                    base_commit=base_commit,
                    source="agent",
                    warnings=warnings,
                )
            except (ValueError, TypeError) as exc:
                snapshot = _fallback_snapshot(
                    base_branch=cfg.base_branch,
                    base_commit=base_commit,
                    warning=f"invalid scout JSON: {exc}",
                )

    baseline = dict(state.get("baseline") or {})
    baseline["repo_scout"] = snapshot.model_dump(mode="json")
    next_state: WorkflowState = {
        **state,
        "baseline": baseline,
        "status": "scouted" if snapshot.source == "agent" else "scout_fallback",
    }
    store = wf._evidence(next_state)
    store.write_json(
        next_state["run_id"],
        "run",
        "repo-scout.json",
        snapshot.model_dump(mode="json"),
    )
    if context_report:
        store.write_json(
            next_state["run_id"],
            "run",
            f"context-scout-{state.get('iteration', 0) + 1:04d}.json",
            context_report,
        )
    store.append_event(
        next_state["run_id"],
        "repo_scout",
        {
            "source": snapshot.source,
            "base_commit": snapshot.base_commit,
            "warnings": snapshot.warnings,
        },
    )
    return next_state


def plan(state: WorkflowState) -> WorkflowState:
    """Plan from immutable requirements plus bounded, explicitly advisory continuity context."""
    cfg = load_config(state["config_path"])
    requirements = wf._requirements(state)
    iteration = state["iteration"] + 1
    snapshot = (state.get("baseline") or {}).get("repo_scout")
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
    prompt = PromptEnvelope(
        core=wf.planner_prompt(requirements, iteration),
        advisory=tuple(advisory),
    )
    store = wf._evidence(state)
    store.write_json(
        state["run_id"],
        "run",
        f"working-memory-{iteration:04d}.json",
        memory,
    )
    result = OpenCodeAdapter(cfg).invoke("planner", prompt, cfg.repo_path)
    if result.context:
        store.write_json(
            state["run_id"],
            "run",
            f"context-plan-{iteration:04d}.json",
            result.context,
        )
    if not result.ok:
        raise RuntimeError(f"Planner failed: {result.output}")
    task = TaskEnvelope.model_validate(wf._json_object(result.output))
    known_ids = {item.id for item in requirements}
    unknown_ids = set(task.requirement_ids) - known_ids
    if unknown_ids:
        raise ValueError(f"Planner returned unknown requirement IDs: {sorted(unknown_ids)}")
    next_state: WorkflowState = {
        **state,
        "task": task.model_dump(mode="json"),
        "iteration": iteration,
        "risk_flags": task.risk_flags,
        "approved_risk_flags": [],
        "tdd_baseline_result": None,
        "tdd_red_result": None,
        "tdd_red_attempts": 0,
        "status": "planned",
    }
    store.write_json(next_state["run_id"], task.id, "task.json", next_state["task"])
    store.append_event(
        next_state["run_id"],
        "planned",
        {
            "task_id": task.id,
            "change_kind": task.change_kind,
            "tdd_mode": task.tdd.mode,
            "scout_base_commit": snapshot.get("base_commit") if snapshot else None,
            "context_budget_status": (
                result.context.get("budget_status") if result.context else None
            ),
        },
    )
    return next_state


def tdd_baseline(state: WorkflowState) -> WorkflowState:
    cfg = load_config(state["config_path"])
    task = TaskEnvelope.model_validate(state["task"])
    evidence = run_tdd_baseline(cfg, Path(state["worktree"]), task)
    payload = evidence.model_dump(mode="json")
    store = wf._evidence(state)
    store.write_json(state["run_id"], task.id, "tdd-baseline.json", payload)
    return {
        **state,
        "tdd_baseline_result": payload,
        "tdd_red_result": None,
        "tdd_red_attempts": 0,
        "status": "tdd_baseline_ready" if evidence.ok else "tdd_baseline_unavailable",
    }


def route_after_tdd_baseline(state: WorkflowState) -> str:
    evidence = _gate_from_state(state, "tdd_baseline_result")
    if evidence is not None and evidence.ok:
        return "continue"
    cfg = load_config(state["config_path"])
    if state.get("replan_attempts", 0) < cfg.max_replans:
        return "replan"
    return "human"


def route_after_build_pause(state: WorkflowState) -> str:
    if state.get("status") == "stopped":
        return "end"
    task = TaskEnvelope.model_validate(state["task"])
    return "tdd_red" if requires_tdd(task) else "build"


def tdd_red_build(state: WorkflowState) -> WorkflowState:
    cfg = load_config(state["config_path"])
    task = TaskEnvelope.model_validate(state["task"])
    prior = state.get("tdd_red_result")
    result = OpenCodeAdapter(cfg).invoke(
        "builder",
        tdd_red_prompt(task, wf._requirements(state), prior),
        Path(state["worktree"]),
    )
    return {
        **state,
        "tdd_red_attempts": state.get("tdd_red_attempts", 0) + 1,
        "message": result.output,
        "status": "tdd_red_prepared" if result.ok else "tdd_red_builder_failed",
    }


def tdd_red_gate(state: WorkflowState) -> WorkflowState:
    cfg = load_config(state["config_path"])
    task = TaskEnvelope.model_validate(state["task"])
    baseline = _gate_from_state(state, "tdd_baseline_result")
    evidence = run_tdd_red(cfg, Path(state["worktree"]), task, baseline)
    payload = evidence.model_dump(mode="json")
    store = wf._evidence(state)
    store.write_json(state["run_id"], task.id, "tdd-red.json", payload)
    store.append_event(
        state["run_id"],
        "tdd_red",
        {"task_id": task.id, "ok": evidence.ok, "attempt": state.get("tdd_red_attempts", 0)},
    )
    return {
        **state,
        "tdd_red_result": payload,
        "status": "tdd_red_verified" if evidence.ok else "tdd_red_failed",
    }


def route_after_tdd_red(state: WorkflowState) -> str:
    evidence = _gate_from_state(state, "tdd_red_result")
    if evidence is not None and evidence.ok:
        return "build"
    cfg = load_config(state["config_path"])
    if state.get("tdd_red_attempts", 0) < cfg.max_repair_attempts:
        return "repair"
    if state.get("replan_attempts", 0) < cfg.max_replans:
        return "replan"
    return "human"


def pause_before_tdd_red_repair(state: WorkflowState) -> WorkflowState:
    return wf._safe_point(state, "before_tdd_red_repair")


def build(state: WorkflowState) -> WorkflowState:
    cfg = load_config(state["config_path"])
    task = TaskEnvelope.model_validate(state["task"])
    result = OpenCodeAdapter(cfg).invoke(
        "builder",
        builder_prompt(
            task,
            wf._requirements(state),
            state.get("tdd_red_result"),
        ),
        Path(state["worktree"]),
    )
    return {
        **state,
        "message": result.output,
        "status": "built" if result.ok else "builder_failed",
    }


def tdd_human_gate(state: WorkflowState) -> WorkflowState:
    decision = interrupt(
        {
            "kind": "tdd_evidence_failure",
            "reason": "Bounded autonomous attempts could not establish valid RED evidence",
            "task": state.get("task"),
            "baseline": state.get("tdd_baseline_result"),
            "red": state.get("tdd_red_result"),
            "allowed": ["replan", "stop"],
        }
    )
    action = decision.get("action") if isinstance(decision, dict) else decision
    human_decisions = wf.record_human_decision(
        state,
        kind="tdd_evidence_failure",
        action=action,
    )
    if action == "replan":
        return {
            **state,
            "human_decisions": human_decisions,
            "replan_attempts": 0,
            "status": "tdd_human_replan",
        }
    if action == "stop":
        return {
            **state,
            "human_decisions": human_decisions,
            "status": "stopped",
            "message": "Stopped at TDD evidence gate",
        }
    raise ValueError(f"Unsupported TDD decision: {action}")


def route_after_tdd_human(state: WorkflowState) -> str:
    return "replan" if state.get("status") == "tdd_human_replan" else "end"


def quality(state: WorkflowState) -> WorkflowState:
    """Run RED-preserving GREEN, repo commands, then final deterministic scope measurement."""
    cfg = load_config(state["config_path"])
    task = TaskEnvelope.model_validate(state["task"])
    worktree = Path(state["worktree"])
    red = _gate_from_state(state, "tdd_red_result")
    results = [
        run_tdd_green(cfg, worktree, task, red),
        *run_quality_gates(cfg, worktree),
        run_scope_gate(cfg, worktree, task),
    ]
    payload = [item.model_dump(mode="json") for item in results]
    store = wf._evidence(state)
    store.write_json(state["run_id"], task.id, "quality.json", payload)
    return {**state, "quality_results": payload, "status": "quality_checked"}


def build_graph(checkpointer: Any = None):
    """Compose the durable LangGraph with Scout and deterministic TDD evidence phases."""
    graph = StateGraph(WorkflowState)
    nodes = [
        ("bootstrap", wf.bootstrap),
        ("guard_plan", wf.guard_spec),
        ("guard_quality", wf.guard_spec),
        ("spec_stop", wf.spec_stop),
        ("pause_plan", wf.pause_before_plan),
        ("pause_build", wf.pause_before_build),
        ("pause_tdd_red_repair", pause_before_tdd_red_repair),
        ("pause_repair", wf.pause_before_repair),
        ("pause_integrate", wf.pause_before_integrate),
        ("pause_pr", wf.pause_before_pr),
        ("pause_merge", wf.pause_before_merge),
        ("scout", scout),
        ("plan", plan),
        ("prepare_worktree", wf.prepare_worktree),
        ("tdd_baseline", tdd_baseline),
        ("tdd_red_build", tdd_red_build),
        ("tdd_red_gate", tdd_red_gate),
        ("tdd_human", tdd_human_gate),
        ("build", build),
        ("quality", quality),
        ("review", wf.review),
        ("repair", wf.repair),
        ("replan", wf.replan),
        ("human", wf.human_gate),
        ("integrate", wf.integrate),
        ("pr", wf.create_pr),
        ("ci", wf.ci_gate),
        ("merge", wf.merge_pr),
        ("refresh", wf.refresh_from_main),
    ]
    for name, node in nodes:
        graph.add_node(name, node)

    graph.add_edge(START, "bootstrap")
    graph.add_edge("bootstrap", "guard_plan")
    graph.add_conditional_edges(
        "guard_plan",
        wf.route_after_guard,
        {"continue": "pause_plan", "end": END},
    )
    graph.add_conditional_edges(
        "pause_plan",
        wf.route_after_pause,
        {"continue": "scout", "end": END},
    )
    graph.add_edge("scout", "plan")
    graph.add_edge("plan", "prepare_worktree")
    graph.add_edge("prepare_worktree", "tdd_baseline")
    graph.add_conditional_edges(
        "tdd_baseline",
        route_after_tdd_baseline,
        {"continue": "pause_build", "replan": "replan", "human": "tdd_human"},
    )
    graph.add_conditional_edges(
        "pause_build",
        route_after_build_pause,
        {"tdd_red": "tdd_red_build", "build": "build", "end": END},
    )
    graph.add_edge("tdd_red_build", "tdd_red_gate")
    graph.add_conditional_edges(
        "tdd_red_gate",
        route_after_tdd_red,
        {
            "build": "build",
            "repair": "pause_tdd_red_repair",
            "replan": "replan",
            "human": "tdd_human",
        },
    )
    graph.add_conditional_edges(
        "pause_tdd_red_repair",
        wf.route_after_pause,
        {"continue": "tdd_red_build", "end": END},
    )
    graph.add_conditional_edges(
        "tdd_human",
        route_after_tdd_human,
        {"replan": "replan", "end": END},
    )
    graph.add_edge("build", "guard_quality")
    graph.add_conditional_edges(
        "guard_quality",
        wf.route_after_guard,
        {"continue": "quality", "end": END},
    )
    graph.add_edge("quality", "review")
    graph.add_conditional_edges(
        "review",
        wf.route_after_review,
        {
            "integrate": "pause_integrate",
            "repair": "pause_repair",
            "replan": "replan",
            "human": "human",
            "spec_stop": "spec_stop",
        },
    )
    graph.add_edge("spec_stop", END)
    graph.add_conditional_edges(
        "pause_repair",
        wf.route_after_pause,
        {"continue": "repair", "end": END},
    )
    graph.add_edge("repair", "guard_quality")
    graph.add_edge("replan", "guard_plan")
    graph.add_conditional_edges(
        "human",
        wf.route_after_human,
        {
            "repair": "pause_repair",
            "prepare": "prepare_worktree",
            "plan": "guard_plan",
            "review": "guard_quality",
            "end": END,
        },
    )
    graph.add_conditional_edges(
        "pause_integrate",
        wf.route_after_pause,
        {"continue": "integrate", "end": END},
    )
    graph.add_conditional_edges(
        "integrate",
        wf.route_after_integrate,
        {"pr": "pause_pr", "end": END},
    )
    graph.add_conditional_edges(
        "pause_pr",
        wf.route_after_pause,
        {"continue": "pr", "end": END},
    )
    graph.add_edge("pr", "ci")
    graph.add_conditional_edges(
        "ci",
        wf.route_after_ci,
        {
            "merge": "pause_merge",
            "repair": "pause_repair",
            "replan": "replan",
            "human": "human",
            "end": END,
        },
    )
    graph.add_conditional_edges(
        "pause_merge",
        wf.route_after_pause,
        {"continue": "merge", "end": END},
    )
    graph.add_edge("merge", "refresh")
    graph.add_conditional_edges(
        "refresh",
        wf.route_after_refresh,
        {"continue": "guard_plan", "human": "human", "end": END},
    )
    return graph.compile(checkpointer=checkpointer)
