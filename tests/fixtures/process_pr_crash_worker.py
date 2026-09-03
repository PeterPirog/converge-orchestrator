from __future__ import annotations

import argparse
import json
import os
import time
from pathlib import Path
from typing import Any, TypedDict

from langgraph.graph import END, START, StateGraph

from converge_orchestrator import runtime, runtime_service, workflow
from converge_orchestrator.config import load_config
from converge_orchestrator.models import PullRequestInfo, TaskEnvelope
from converge_orchestrator.runtime_service import ScheduledRunController


class ChaosState(TypedDict, total=False):
    project_id: str
    config_path: str
    run_id: str
    thread_id: str
    requirements_hash: str
    task: dict[str, Any]
    quality_results: list[dict[str, Any]]
    risk_flags: list[str]
    risk_fingerprint: str
    branch: str
    pr: dict[str, Any]
    status: str


class DurableFakeGitHubAdapter:
    """Process-external PR store used to model an idempotent GitHub ensure boundary."""

    def __init__(self, cfg: Any):
        self.cfg = cfg
        self.path = cfg.state_dir / "fake-github-pr.json"
        self.creates_path = cfg.state_dir / "fake-github-pr-creates.txt"

    def get_pull_request(self, number: int) -> PullRequestInfo:
        payload = json.loads(self.path.read_text(encoding="utf-8"))
        if int(payload["number"]) != number:
            raise RuntimeError(f"unknown fake PR {number}")
        return PullRequestInfo.model_validate(payload)

    def ensure_pull_request(
        self,
        *,
        head: str,
        base: str,
        title: str,
        body: str,
    ) -> PullRequestInfo:
        if self.path.exists():
            return PullRequestInfo.model_validate_json(self.path.read_text(encoding="utf-8"))

        creates = int(self.creates_path.read_text(encoding="utf-8")) if self.creates_path.exists() else 0
        creates += 1
        self.creates_path.write_text(str(creates), encoding="utf-8")
        pr = PullRequestInfo(
            number=17,
            url="https://github.invalid/example/repo/pull/17",
            head_sha="candidate-sha",
            state="open",
        )
        self.path.write_text(pr.model_dump_json(indent=2) + "\n", encoding="utf-8")
        metadata = {
            "head": head,
            "base": base,
            "title": title,
            "body": body,
        }
        (self.cfg.state_dir / "fake-github-pr-request.json").write_text(
            json.dumps(metadata, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        return pr


def _seed(state: ChaosState) -> dict[str, Any]:
    task = TaskEnvelope(
        id="CHAOS-PR-001",
        requirement_ids=["ARCH-001"],
        title="Recover PR creation",
        objective="Reuse a PR created before the LangGraph checkpoint",
        allowed_paths=["src/**"],
        change_kind="other",
    )
    return {
        **state,
        "requirements_hash": "immutable-test-hash",
        "task": task.model_dump(mode="json"),
        "quality_results": [],
        "risk_flags": [],
        "risk_fingerprint": "candidate-risk-sha",
        "branch": "converge/chaos-pr-001",
        "status": "seeded",
    }


def _create_pr_then_crash(state: ChaosState) -> dict[str, Any]:
    workflow.GitHubAdapter = DurableFakeGitHubAdapter
    next_state = workflow.create_pr(state)
    cfg = load_config(state["config_path"])
    attempts_path = cfg.state_dir / "pr-chaos-attempts.txt"
    attempts = int(attempts_path.read_text(encoding="utf-8")) if attempts_path.exists() else 0
    attempts += 1
    attempts_path.write_text(str(attempts), encoding="utf-8")
    if attempts == 1:
        # The fake remote PR already exists, but LangGraph has not checkpointed `next_state` yet.
        os._exit(93)
    return next_state


def _finish(_state: ChaosState) -> dict[str, str]:
    return {"status": "completed"}


def build_chaos_graph(checkpointer: Any = None):
    graph = StateGraph(ChaosState)
    graph.add_node("seed", _seed)
    graph.add_node("create_pr", _create_pr_then_crash)
    graph.add_node("finish", _finish)
    graph.add_edge(START, "seed")
    graph.add_edge("seed", "create_pr")
    graph.add_edge("create_pr", "finish")
    graph.add_edge("finish", END)
    return graph.compile(checkpointer=checkpointer)


def _controller(registry_path: Path) -> ScheduledRunController:
    runtime._LEASE_TTL_SECONDS = 1
    runtime_service.build_graph = build_chaos_graph
    return ScheduledRunController(registry_path)


def crash(registry_path: Path, config_path: Path) -> int:
    controller = _controller(registry_path)
    controller.register_project("chaos-pr", config_path)
    controller.start_run("chaos-pr")
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        records = controller.registry.runs_for_project("chaos-pr")
        if records and records[0]["finished_at"]:
            raise RuntimeError(f"run finished instead of crashing: {records[0]}")
        time.sleep(0.02)
    raise RuntimeError("PR chaos node did not terminate the process")


def recover(registry_path: Path) -> int:
    controller = _controller(registry_path)
    deadline = time.monotonic() + 15
    while time.monotonic() < deadline:
        record = controller.registry.runs_for_project("chaos-pr")[0]
        if record["finished_at"]:
            if record["status"] != "completed":
                raise RuntimeError(f"recovered run failed: {record}")
            return 0
        time.sleep(0.05)
    raise RuntimeError("recovered PR run did not reach a terminal checkpoint")


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
