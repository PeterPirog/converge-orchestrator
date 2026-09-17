from __future__ import annotations

import threading
import time
from pathlib import Path
from typing import TypedDict

from langgraph.graph import END, START, StateGraph

from converge_orchestrator.persistence import PersistenceBackend
from converge_orchestrator.runtime_service import ScheduledRunController

_SLOW_NODE_SECONDS = 2.0
_STATUS_LIVENESS_LIMIT_SECONDS = 1.0


def _build_sleep_graph(checkpointer, node_started) -> object:  # noqa: ANN001
    """A tiny durable graph whose single node simulates a minutes-long model invocation."""

    class _State(TypedDict):
        steps: int

    def slow(state: _State) -> dict:
        node_started.set()
        time.sleep(_SLOW_NODE_SECONDS)
        return {"steps": state.get("steps", 0) + 1}

    graph = StateGraph(_State)
    graph.add_node("slow", slow)
    graph.add_edge(START, "slow")
    graph.add_edge("slow", END)
    return graph.compile(checkpointer=checkpointer)


def _controller(tmp_path: Path) -> ScheduledRunController:
    controller = object.__new__(ScheduledRunController)
    # Production always materializes cfg.state_dir before open_checkpointer (config.load_config);
    # mirror that contract here so the SQLite connection target exists.
    (tmp_path / "state").mkdir(parents=True, exist_ok=True)
    persistence = PersistenceBackend(tmp_path / "control.sqlite")
    controller.persistence = persistence
    controller.registry = persistence.registry
    controller._workers = {}
    controller._lock = threading.Lock()
    controller._lease_owner = "controller-test"
    controller._timers = {}
    controller._timer_generations = {}
    controller._config_for_run = lambda record: None  # type: ignore[method-assign]
    controller._enforce_submission_budget = lambda record: None  # type: ignore[method-assign]
    node_started = threading.Event()

    def _open_graph(record: dict):
        checkpointer, db = persistence.open_checkpointer(tmp_path / "state")
        graph = _build_sleep_graph(checkpointer, node_started)
        graph_config = {"configurable": {"thread_id": record["thread_id"]}}
        return graph, db, graph_config

    controller._open_graph = _open_graph  # type: ignore[method-assign]
    return controller, node_started


def test_long_model_invocation_keeps_status_responsive(tmp_path: Path, monkeypatch) -> None:
    """Test D: a long in-graph model op must never stall the status path.

    The workflow runs in its own daemon thread and must not hold the controller lock or block
    the checkpoint database for readers while a model invocation runs. Every status poll taken
    during the sleeping node must complete quickly and observe worker_alive=True.
    """
    monkeypatch.delenv("CONVERGE_DATABASE_URL", raising=False)
    monkeypatch.delenv("LANGGRAPH_STRICT_MSGPACK", raising=False)
    controller, node_started = _controller(tmp_path)
    controller.registry.register_project(
        "project", tmp_path / "converge.yaml", workspace_id="w", state_store_id="s"
    )
    controller.registry.create_run("run-d", "project", "thread-d")

    controller._submit("run-d", {"steps": 0})

    polls: list[float] = []
    observed_worker_alive: list[bool] = []
    deadline = time.monotonic() + 20.0
    while time.monotonic() < deadline:
        started = time.monotonic()
        status = controller.status("run-d")
        elapsed = time.monotonic() - started
        assert elapsed < _STATUS_LIVENESS_LIMIT_SECONDS, (
            f"status poll took {elapsed:.2f}s during model execution"
        )
        polls.append(elapsed)
        observed_worker_alive.append(bool(status.get("worker_alive")))
        worker = controller._workers.get("run-d")
        if worker is None or not worker.is_alive():
            break
        time.sleep(0.2)

    worker = controller._workers.get("run-d")
    if worker is not None:
        worker.join(timeout=15)
        assert not worker.is_alive(), "graph invoke did not finish"

    final_status = controller.status("run-d")
    assert final_status["values"] == {"steps": 1}
    assert final_status["finished_at"]
    # The liveness invariant was actually exercised: polls happened while the node was sleeping.
    assert node_started.is_set()
    assert len(polls) >= 3
    assert any(observed_worker_alive)
