from __future__ import annotations

import argparse
import os
import time
from pathlib import Path
from typing import Any, TypedDict

from langgraph.graph import END, START, StateGraph

from converge_orchestrator import runtime, runtime_service
from converge_orchestrator.config import load_config
from converge_orchestrator.git import (
    commit_all,
    create_worktree,
    existing_candidate_commit,
    push,
)
from converge_orchestrator.runtime_service import ScheduledRunController


class ChaosState(TypedDict, total=False):
    project_id: str
    config_path: str
    run_id: str
    thread_id: str
    worktree: str
    branch: str
    commit_sha: str
    status: str


def _integrate_then_crash(state: ChaosState) -> dict[str, Any]:
    cfg = load_config(state["config_path"])
    worktree, branch = create_worktree(
        cfg.repo_path,
        cfg.worktree_dir,
        "CHAOS-INTEGRATE-001",
        cfg.base_branch,
        cfg.branch_prefix,
    )
    marker = worktree / "candidate.txt"
    if not marker.exists():
        marker.write_text("candidate survives integration crash\n", encoding="utf-8")

    created_commit = commit_all(worktree, "feat: integration crash candidate")
    commit = created_commit or existing_candidate_commit(worktree, cfg.base_branch)
    if commit is None:
        raise RuntimeError("candidate commit was not created or recoverable")
    push(worktree, branch)

    attempts_path = cfg.state_dir / "integration-chaos-attempts.txt"
    attempts = int(attempts_path.read_text(encoding="utf-8")) if attempts_path.exists() else 0
    attempts += 1
    attempts_path.write_text(str(attempts), encoding="utf-8")
    if attempts == 1:
        # Kill the process after commit+push side effects but before LangGraph checkpoints
        # this node's returned commit/worktree state.
        os._exit(92)

    return {
        "worktree": str(worktree),
        "branch": branch,
        "commit_sha": commit,
        "status": "integration_recovered",
    }


def _finish(_state: ChaosState) -> dict[str, str]:
    return {"status": "completed"}


def build_chaos_graph(checkpointer: Any = None):
    graph = StateGraph(ChaosState)
    graph.add_node("integrate", _integrate_then_crash)
    graph.add_node("finish", _finish)
    graph.add_edge(START, "integrate")
    graph.add_edge("integrate", "finish")
    graph.add_edge("finish", END)
    return graph.compile(checkpointer=checkpointer)


def _controller(registry_path: Path) -> ScheduledRunController:
    runtime._LEASE_TTL_SECONDS = 1
    runtime_service.build_graph = build_chaos_graph
    return ScheduledRunController(registry_path)


def crash(registry_path: Path, config_path: Path) -> int:
    controller = _controller(registry_path)
    controller.register_project("chaos-integration", config_path)
    controller.start_run("chaos-integration")
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        records = controller.registry.runs_for_project("chaos-integration")
        if records and records[0]["finished_at"]:
            raise RuntimeError(f"run finished instead of crashing: {records[0]}")
        time.sleep(0.02)
    raise RuntimeError("integration chaos node did not terminate the process")


def recover(registry_path: Path) -> int:
    controller = _controller(registry_path)
    deadline = time.monotonic() + 15
    while time.monotonic() < deadline:
        record = controller.registry.runs_for_project("chaos-integration")[0]
        if record["finished_at"]:
            if record["status"] != "completed":
                raise RuntimeError(f"recovered run failed: {record}")
            return 0
        time.sleep(0.05)
    raise RuntimeError("recovered integration run did not reach a terminal checkpoint")


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
