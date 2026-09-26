from __future__ import annotations

import argparse
import json
import os
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, TypedDict

from langgraph.graph import END, START, StateGraph

from converge_orchestrator import runtime_service
from converge_orchestrator.config import load_config
from converge_orchestrator.control import ControlSignals
from converge_orchestrator.runtime_service import ScheduledRunController
from converge_orchestrator.workflow import pause_before_reviewer_recovery_wait


class ReviewerRecoveryState(TypedDict, total=False):
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


def _trigger_reviewer_recovery_wait(state: ReviewerRecoveryState) -> dict[str, Any]:
    """Set up state to trigger reviewer_recovery_wait interrupt."""
    # Always trigger the wait on first entry (the crash test only runs once)
    wake_at = (datetime.now(UTC) + timedelta(seconds=5)).isoformat()
    return {
        **state,
        "status": "reviewer_recovery_wait",
        "reviewer_recovery_wake_at": wake_at,
        "review_execution_retries": 1,
    }


def _finish(state: ReviewerRecoveryState) -> dict[str, str]:
    cfg = load_config(state["config_path"])
    marker = {
        "run_id": state["run_id"],
        "thread_id": state["thread_id"],
        "finished_at": datetime.now(UTC).isoformat(),
    }
    (cfg.state_dir / "reviewer-recovery-wait-finished.json").write_text(
        json.dumps(marker, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return {"status": "completed"}


def build_chaos_graph(checkpointer: Any = None):
    graph = StateGraph(ReviewerRecoveryState)
    graph.add_node("trigger_wait", _trigger_reviewer_recovery_wait)
    graph.add_node("pause_reviewer_recovery_wait", pause_before_reviewer_recovery_wait)
    graph.add_node("finish", _finish)
    graph.add_edge(START, "trigger_wait")

    def _route(s):
        status = s.get("status")
        return "pause_reviewer_recovery_wait" if status == "reviewer_recovery_wait" else "finish"

    graph.add_conditional_edges(
        "trigger_wait",
        _route,
        {"pause_reviewer_recovery_wait": "pause_reviewer_recovery_wait", "finish": "finish"},
    )
    graph.add_edge("pause_reviewer_recovery_wait", "finish")
    graph.add_edge("finish", END)
    return graph.compile(checkpointer=checkpointer)


def _controller(registry_path: Path) -> ScheduledRunController:
    runtime_service.build_graph = build_chaos_graph
    return ScheduledRunController(registry_path)


def crash(registry_path: Path, config_path: Path) -> int:
    controller = _controller(registry_path)
    controller.register_project("chaos-reviewer-recovery", config_path)
    record = controller.start_run("chaos-reviewer-recovery")
    run_id = str(record["id"])
    
    # Request pause so that pause_before_reviewer_recovery_wait will call interrupt
    project = controller.registry.get_project("chaos-reviewer-recovery")
    cfg = load_config(project["config_path"])
    signals = ControlSignals(cfg.state_dir)
    signals.request_pause(run_id)
    
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        current = controller.registry.get_run(run_id)
        if current["finished_at"]:
            raise RuntimeError(
                f"Reviewer recovery wait run finished before service crash: {current}"
            )
        if current["status"] == "waiting_review":
            payload = controller.interrupt(run_id)
            if not payload or payload.get("kind") != "reviewer_recovery_wait":
                raise RuntimeError(
                    f"waiting_review without durable reviewer_recovery_wait interrupt: {payload}"
                )
            project = controller.registry.get_project("chaos-reviewer-recovery")
            cfg = load_config(project["config_path"])
            snapshot = {
                "run_id": run_id,
                "thread_id": current["thread_id"],
                "interrupt": payload,
            }
            (cfg.state_dir / "reviewer-recovery-wait-interrupt.json").write_text(
                json.dumps(snapshot, indent=2, sort_keys=True) + "\n",
                encoding="utf-8",
            )
            # Kill the whole service while the machine-managed timer only exists in memory.
            os._exit(94)
        time.sleep(0.01)
    raise RuntimeError("run did not reach durable reviewer recovery wait before timeout")


def recover(registry_path: Path) -> int:
    controller = _controller(registry_path)
    deadline = time.monotonic() + 12
    while time.monotonic() < deadline:
        record = controller.registry.runs_for_project("chaos-reviewer-recovery")[0]
        if record["finished_at"]:
            if record["status"] != "completed":
                raise RuntimeError(f"recovered reviewer recovery wait run failed: {record}")
            return 0
        time.sleep(0.05)
    raise RuntimeError("restored reviewer recovery wait did not resume automatically")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("mode", choices=("crash", "recover"))
    parser.add_argument("registry_path", type=Path)
    parser.add_argument("config_path", nargs="?", type=Path)
    args = parser.parse_args()
    if args.mode == "crash":
        if args.config_path is None:
            parser.error("crash mode requires config_path")
        return crash(args.registry_path, args.config_path)
    return recover(args.registry_path)


if __name__ == "__main__":
    raise SystemExit(main())