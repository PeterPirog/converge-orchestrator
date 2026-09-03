from __future__ import annotations

from typing import Any

from langgraph.graph import END, START, StateGraph

from . import workflow as wf
from .ci import ci_poll, ci_wait, route_after_ci
from .graph import (
    build,
    pause_before_tdd_red_repair,
    plan,
    quality,
    route_after_build_pause,
    route_after_tdd_baseline,
    route_after_tdd_human,
    route_after_tdd_red,
    scout,
    tdd_baseline,
    tdd_human_gate,
    tdd_red_build,
    tdd_red_gate,
)
from .models import WorkflowState


def build_graph(checkpointer: Any = None):
    """Compose the durable service graph with checkpointable machine CI waits."""
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
        ("ci_poll", ci_poll),
        ("ci_wait", ci_wait),
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
    graph.add_edge("pr", "ci_poll")
    graph.add_conditional_edges(
        "ci_poll",
        route_after_ci,
        {
            "wait": "ci_wait",
            "merge": "pause_merge",
            "repair": "pause_repair",
            "replan": "replan",
            "human": "human",
            "end": END,
        },
    )
    graph.add_edge("ci_wait", "ci_poll")
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
