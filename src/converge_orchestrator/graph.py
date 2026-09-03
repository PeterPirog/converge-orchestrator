from __future__ import annotations

import json
from typing import Any, Literal

from langgraph.graph import END, START, StateGraph
from pydantic import BaseModel, Field

from . import workflow as wf
from .config import load_config
from .git import update_base
from .models import Requirement, TaskEnvelope, WorkflowState
from .opencode import OpenCodeAdapter
from .prompts import contract_excerpt


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


def scout(state: WorkflowState) -> WorkflowState:
    """Refresh canonical base and produce a bounded read-only repository map."""
    cfg = load_config(state["config_path"])
    base_commit = update_base(cfg.repo_path, cfg.base_branch)
    requirements = wf._requirements(state)

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
    """Plan from immutable requirements plus the latest scout snapshot."""
    cfg = load_config(state["config_path"])
    requirements = wf._requirements(state)
    prompt = wf.planner_prompt(requirements, state["iteration"] + 1)
    snapshot = (state.get("baseline") or {}).get("repo_scout")
    if snapshot:
        prompt += (
            "\nREPOSITORY SCOUT SNAPSHOT (advisory, verify details when needed):\n"
            + json.dumps(snapshot, ensure_ascii=False, indent=2)
            + "\n"
        )
    result = OpenCodeAdapter(cfg).invoke("planner", prompt, cfg.repo_path)
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
        "iteration": state["iteration"] + 1,
        "risk_flags": task.risk_flags,
        "approved_risk_flags": [],
        "status": "planned",
    }
    store = wf._evidence(next_state)
    store.write_json(next_state["run_id"], task.id, "task.json", next_state["task"])
    store.append_event(
        next_state["run_id"],
        "planned",
        {
            "task_id": task.id,
            "scout_base_commit": snapshot.get("base_commit") if snapshot else None,
        },
    )
    return next_state


def build_graph(checkpointer: Any = None):
    """Compose the durable LangGraph with a read-only scout immediately before planning."""
    graph = StateGraph(WorkflowState)
    nodes = [
        ("bootstrap", wf.bootstrap),
        ("guard_plan", wf.guard_spec),
        ("guard_quality", wf.guard_spec),
        ("spec_stop", wf.spec_stop),
        ("pause_plan", wf.pause_before_plan),
        ("pause_build", wf.pause_before_build),
        ("pause_repair", wf.pause_before_repair),
        ("pause_integrate", wf.pause_before_integrate),
        ("pause_pr", wf.pause_before_pr),
        ("pause_merge", wf.pause_before_merge),
        ("scout", scout),
        ("plan", plan),
        ("prepare_worktree", wf.prepare_worktree),
        ("build", wf.build),
        ("quality", wf.quality),
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
    graph.add_edge("prepare_worktree", "pause_build")
    graph.add_conditional_edges(
        "pause_build",
        wf.route_after_pause,
        {"continue": "build", "end": END},
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
