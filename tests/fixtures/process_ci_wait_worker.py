from __future__ import annotations

import argparse
import json
import os
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, TypedDict

from langgraph.graph import END, START, StateGraph

from converge_orchestrator import runtime_service
from converge_orchestrator.ci import ci_wait
from converge_orchestrator.config import load_config
from converge_orchestrator.models import CIResult
from converge_orchestrator.runtime_service import ScheduledRunController


class ChaosState(TypedDict, total=False):
    project_id: str
    config_path: str
    run_id: str
    thread_id: str
    ci: dict[str, Any]
    status: str


def _wait_for_ci(state: ChaosState) -> dict[str, Any]:
    prepared: ChaosState = {
        **state,
        "ci": CIResult(
            status="pending",
            head_sha="candidate-sha",
        ).model_dump(mode="json"),
        "status": "ci_pending",
    }
    return ci_wait(prepared)


def _finish(state: ChaosState) -> dict[str, str]:
    cfg = load_config(state["config_path"])
    marker = {
        "run_id": state["run_id"],
        "thread_id": state["thread_id"],
        "finished_at": datetime.now(UTC).isoformat(),
    }
    (cfg.state_dir / "ci-wait-finished.json").write_text(
        json.dumps(marker, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return {"status": "completed"}


def build_chaos_graph(checkpointer: Any = None):
    graph = StateGraph(ChaosState)
    graph.add_node("ci_wait", _wait_for_ci)
    graph.add_node("finish", _finish)
    graph.add_edge(START, "ci_wait")
    graph.add_edge("ci_wait", "finish")
    graph.add_edge("finish", END)
    return graph.compile(checkpointer=checkpointer)


def _controller(registry_path: Path) -> ScheduledRunController:
    runtime_service.build_graph = build_chaos_graph
    return ScheduledRunController(registry_path)


def crash(registry_path: Path, config_path: Path) -> int:
    controller = _controller(registry_path)
    controller.register_project("chaos-ci", config_path)
    record = controller.start_run("chaos-ci")
    run_id = str(record["id"])
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        current = controller.registry.get_run(run_id)
        if current["finished_at"]:
            raise RuntimeError(f"CI-wait run finished before service crash: {current}")
        if current["status"] == "waiting_ci":
            payload = controller.interrupt(run_id)
            if not payload or payload.get("kind") != "ci_wait":
                raise RuntimeError(f"waiting_ci without durable CI interrupt: {payload}")
            project = controller.registry.get_project("chaos-ci")
            cfg = load_config(project["config_path"])
            snapshot = {
                "run_id": run_id,
                "thread_id": current["thread_id"],
                "interrupt": payload,
            }
            (cfg.state_dir / "ci-wait-interrupt.json").write_text(
                json.dumps(snapshot, indent=2, sort_keys=True) + "\n",
                encoding="utf-8",
            )
            # Kill the whole service while the machine-managed timer only exists in memory.
            os._exit(94)
        time.sleep(0.01)
    raise RuntimeError("run did not reach durable CI wait before timeout")


def recover(registry_path: Path) -> int:
    controller = _controller(registry_path)
    deadline = time.monotonic() + 12
    while time.monotonic() < deadline:
        record = controller.registry.runs_for_project("chaos-ci")[0]
        if record["finished_at"]:
            if record["status"] != "completed":
                raise RuntimeError(f"recovered CI-wait run failed: {record}")
            return 0
        time.sleep(0.05)
    raise RuntimeError("restored CI wait did not resume automatically")


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
