from __future__ import annotations

import argparse
import json
import time
from pathlib import Path
from typing import Any, TypedDict

from langgraph.graph import END, START, StateGraph

from converge_orchestrator import runtime_service
from converge_orchestrator.config import load_config
from converge_orchestrator.runtime_service import ScheduledRunController
from converge_orchestrator.workflow import (
    pause_before_reviewer_recovery,
    pause_before_reviewer_recovery_wait,
    review_execution_failure,
    reviewer_recovery,
    route_after_review,
)


class ExhaustionState(TypedDict, total=False):
    project_id: str
    config_path: str
    run_id: str
    thread_id: str
    task: dict[str, Any]
    worktree: str
    branch: str
    quality_results: list[dict[str, Any]]
    compliance: dict[str, Any]
    review_result: dict[str, Any] | None
    repair_attempts: int
    replan_attempts: int
    review_execution_retries: int
    risk_flags: list[str]
    approved_risk_flags: list[str]
    risk_report: dict[str, Any] | None
    risk_fingerprint: str | None
    human_decisions: list[dict[str, Any]]
    status: str
    reviewer_recovery_wake_at: str | None


def _trigger_transport_failure(state: ExhaustionState) -> dict[str, Any]:
    """First call - simulate transport failure in review."""
    # This simulates the review() node detecting a transport failure
    # and returning review_transport_failure status with retries=1
    return {
        **state,
        "status": "review_transport_failure",
        "review_execution_retries": 1,
    }


def _trigger_transport_failure_again(state: ExhaustionState) -> dict[str, Any]:
    """Second call - reviewer_recovery also fails with transport."""
    # This simulates reviewer_recovery also failing with transport
    # With max_review_execution_retries=1, this should exhaust the budget
    retries = state.get("review_execution_retries", 0) + 1
    return {
        **state,
        "status": "review_transport_failure",
        "review_execution_retries": retries,
    }


def _marker(state: ExhaustionState) -> dict[str, Any]:
    """Write exhaustion marker and finish."""
    cfg = load_config(state["config_path"])
    marker = {
        "run_id": state["run_id"],
        "thread_id": state["thread_id"],
        "status": state.get("status"),
        "repair_attempts": state.get("repair_attempts", 0),
        "replan_attempts": state.get("replan_attempts", 0),
        "review_execution_retries": state.get("review_execution_retries", 0),
    }
    (cfg.state_dir / "reviewer-execution-exhaustion.json").write_text(
        json.dumps(marker, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return {"status": state.get("status", "failed")}


def build_chaos_graph(checkpointer=None):
    graph = StateGraph(ExhaustionState)
    # Node 1: Initial transport failure (simulates review() detecting transport failure)
    graph.add_node("trigger_failure", _trigger_transport_failure)
    # Node 2: reviewer_recovery node (will fail again with transport)
    graph.add_node("reviewer_recovery", reviewer_recovery)
    # Node 3: pause_before_reviewer_recovery_wait (machine wait)
    graph.add_node("pause_reviewer_recovery", pause_before_reviewer_recovery)
    graph.add_node("pause_reviewer_recovery_wait", pause_before_reviewer_recovery_wait)
    # Node 4: review_execution_failure (terminal)
    graph.add_node("review_execution_failure", review_execution_failure)
    # Node 5: Marker writer
    graph.add_node("marker", _marker)

    graph.add_edge(START, "trigger_failure")
    
    # After first transport failure, route to reviewer_recovery
    def _route_failure(s):
        return "reviewer_recovery" if s.get("status") == "review_transport_failure" else "marker"

    graph.add_conditional_edges(
        "trigger_failure",
        _route_failure,
        {"reviewer_recovery": "reviewer_recovery", "marker": "marker"},
    )
    
    # reviewer_recovery will route based on its internal logic
    # It calls route_after_review internally
    graph.add_conditional_edges(
        "reviewer_recovery",
        route_after_review,
        {
            "integrate": "pause_reviewer_recovery",
            "reviewer_recovery": "pause_reviewer_recovery",
            "replan": "marker",
            "human": "marker",
            "spec_stop": "marker",
            "pause_before_reviewer_recovery_wait": "pause_reviewer_recovery_wait",
            "review_execution_failure": "review_execution_failure",
        },
    )
    
    # pause_before_reviewer_recovery routes to continue or end
    graph.add_conditional_edges(
        "pause_reviewer_recovery",
        lambda s: "reviewer_recovery" if s.get("status") != "stopped" else "marker",
        {"reviewer_recovery": "reviewer_recovery", "marker": "marker"},
    )
    
    # pause_before_reviewer_recovery_wait routes to continue or end
    graph.add_conditional_edges(
        "pause_reviewer_recovery_wait",
        lambda s: "reviewer_recovery" if s.get("status") != "stopped" else "marker",
        {"reviewer_recovery": "reviewer_recovery", "marker": "marker"},
    )
    
    # review_execution_failure goes to marker
    graph.add_edge("review_execution_failure", "marker")
    graph.add_edge("marker", END)
    
    return graph.compile(checkpointer=checkpointer)


def _controller(registry_path: Path) -> ScheduledRunController:
    runtime_service.build_graph = build_chaos_graph
    return ScheduledRunController(registry_path)


def run_exhaustion(registry_path: Path, config_path: Path) -> int:
    controller = _controller(registry_path)
    controller.register_project("chaos-reviewer-exhaustion", config_path)
    record = controller.start_run("chaos-reviewer-exhaustion")
    run_id = str(record["id"])
    
    # Request pause for the reviewer recovery wait
    project = controller.registry.get_project("chaos-reviewer-exhaustion")
    cfg = load_config(project["config_path"])
    from converge_orchestrator.control import ControlSignals
    signals = ControlSignals(cfg.state_dir)
    signals.request_pause(run_id)
    
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline:
        current = controller.registry.get_run(run_id)
        if current["finished_at"]:
            if current["status"] != "failed":
                raise RuntimeError(f"Expected failed status, got: {current}")
            return 0
        time.sleep(0.1)
    raise RuntimeError("Reviewer execution exhaustion did not complete")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("registry_path", type=Path)
    parser.add_argument("config_path", type=Path)
    args = parser.parse_args()
    return run_exhaustion(args.registry_path, args.config_path)


if __name__ == "__main__":
    raise SystemExit(main())