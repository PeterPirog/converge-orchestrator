from __future__ import annotations

import json
import sqlite3
import threading
from pathlib import Path
from typing import Any
from uuid import uuid4

from langgraph.checkpoint.sqlite import SqliteSaver
from langgraph.types import Command

from .config import load_config
from .control import ControlSignals
from .evidence import EvidenceStore
from .graph import build_graph
from .registry import ControlRegistry
from .workflow import bootstrap

_TERMINAL_STATUSES = {
    "completed",
    "converged",
    "failed",
    "no_changes",
    "spec_changed",
    "stopped",
    "ci_pass",
}
_ACTIVE_STATUSES = {"queued", "running", "pause_requested", "paused", "interrupted", "recoverable"}


def _interrupt_payload(snapshot: Any) -> dict[str, Any] | None:
    interrupts = list(getattr(snapshot, "interrupts", ()) or ())
    for task in getattr(snapshot, "tasks", ()) or ():
        interrupts.extend(getattr(task, "interrupts", ()) or ())
    for item in interrupts:
        value = getattr(item, "value", item)
        if isinstance(value, dict):
            return value
        return {"kind": "human", "value": value}
    return None


class RunController:
    """Coordinates API requests with durable LangGraph checkpoints and run metadata."""

    def __init__(self, registry_path: Path):
        self.registry = ControlRegistry(registry_path)
        self._workers: dict[str, threading.Thread] = {}
        self._lock = threading.Lock()

    def register_project(self, project_id: str, config_path: Path) -> dict[str, Any]:
        cfg = load_config(config_path)
        if not cfg.repo_path.is_dir() or not (cfg.repo_path / ".git").exists():
            raise ValueError(f"Not a git repository: {cfg.repo_path}")
        if not cfg.requirements_path.is_file():
            raise ValueError(f"Requirements file not found: {cfg.requirements_path}")
        return self.registry.register_project(project_id, config_path)

    def bootstrap_project(self, project_id: str) -> dict[str, Any]:
        project = self.registry.get_project(project_id)
        run_id = f"bootstrap-{uuid4().hex}"
        state = bootstrap(
            {
                "project_id": project_id,
                "config_path": project["config_path"],
                "run_id": run_id,
                "thread_id": run_id,
            }
        )
        self.registry.set_requirements_hash(project_id, state["requirements_hash"])
        return state

    def start_run(self, project_id: str) -> dict[str, Any]:
        project = self.registry.get_project(project_id)
        for existing in self.registry.runs_for_project(project_id):
            if existing["status"] in _ACTIVE_STATUSES and not existing["finished_at"]:
                raise RuntimeError(f"Project already has active run {existing['id']}")
        run_id = uuid4().hex
        thread_id = f"run-{run_id}"
        record = self.registry.create_run(run_id, project_id, thread_id)
        initial: dict[str, Any] = {
            "project_id": project_id,
            "config_path": project["config_path"],
            "run_id": run_id,
            "thread_id": thread_id,
        }
        self._submit(run_id, initial)
        return record

    def request_pause(self, run_id: str) -> dict[str, Any]:
        record = self.registry.get_run(run_id)
        if record["finished_at"]:
            raise RuntimeError("Cannot pause a finished run")
        project = self.registry.get_project(record["project_id"])
        cfg = load_config(project["config_path"])
        ControlSignals(cfg.state_dir).request_pause(run_id)
        self.registry.update_run(run_id, status="pause_requested")
        return self.status(run_id)

    def resume(self, run_id: str) -> dict[str, Any]:
        record = self.registry.get_run(run_id)
        snapshot = self._snapshot(record)
        interrupt_payload = snapshot.get("interrupt")
        if interrupt_payload and interrupt_payload.get("kind") != "controlled_pause":
            raise RuntimeError("Run requires /decision, not /resume")
        if interrupt_payload:
            project = self.registry.get_project(record["project_id"])
            cfg = load_config(project["config_path"])
            ControlSignals(cfg.state_dir).clear_pause(run_id)
            self._submit(run_id, Command(resume="resume"))
        elif snapshot.get("next"):
            self._submit(run_id, None)
        else:
            raise RuntimeError("Run has no resumable checkpoint")
        return self.registry.get_run(run_id)

    def decide(self, run_id: str, decision: dict[str, Any]) -> dict[str, Any]:
        record = self.registry.get_run(run_id)
        snapshot = self._snapshot(record)
        interrupt_payload = snapshot.get("interrupt")
        if not interrupt_payload:
            raise RuntimeError("Run is not waiting for a human decision")
        if interrupt_payload.get("kind") == "controlled_pause":
            raise RuntimeError("Controlled pause must be resumed with /resume")
        self._submit(run_id, Command(resume=decision))
        return self.registry.get_run(run_id)

    def status(self, run_id: str) -> dict[str, Any]:
        record = self.registry.get_run(run_id)
        snapshot = self._snapshot(record)
        with self._lock:
            worker_alive = bool(self._workers.get(run_id) and self._workers[run_id].is_alive())
        status = record["status"]
        if snapshot.get("interrupt"):
            kind = snapshot["interrupt"].get("kind")
            status = "paused" if kind == "controlled_pause" else "interrupted"
        elif snapshot.get("next") and not worker_alive and status in {"queued", "running"}:
            status = "recoverable"
        elif snapshot.get("values") and not snapshot.get("next") and not worker_alive:
            status = snapshot["values"].get("status", status)
        task = snapshot.get("values", {}).get("task")
        active_task_id = task.get("id") if isinstance(task, dict) else None
        node = snapshot["next"][0] if snapshot.get("next") else None
        self.registry.update_run(
            run_id,
            status=status,
            node=node or "done",
            active_task_id=active_task_id,
        )
        result = self.registry.get_run(run_id)
        result.update(snapshot)
        result["worker_alive"] = worker_alive
        return result

    def interrupt(self, run_id: str) -> dict[str, Any] | None:
        record = self.registry.get_run(run_id)
        return self._snapshot(record).get("interrupt")

    def compliance(self, project_id: str) -> dict[str, Any]:
        project = self.registry.get_project(project_id)
        cfg = load_config(project["config_path"])
        path = cfg.state_dir / "compliance.json"
        if not path.is_file():
            raise FileNotFoundError(path)
        return json.loads(path.read_text(encoding="utf-8"))

    def evidence(self, task_id: str) -> list[dict[str, Any]]:
        matches: list[dict[str, Any]] = []
        for project in self.registry.list_projects():
            cfg = load_config(project["config_path"])
            store = EvidenceStore(cfg.state_dir / "evidence")
            for bundle in store.find_task_bundles(task_id):
                matches.append({"project_id": project["id"], **bundle})
        return matches

    def _submit(self, run_id: str, input_value: Any) -> None:
        with self._lock:
            existing = self._workers.get(run_id)
            if existing and existing.is_alive():
                raise RuntimeError("Run is already executing")
            worker = threading.Thread(
                target=self._execute,
                args=(run_id, input_value),
                name=f"converge-{run_id[:8]}",
                daemon=True,
            )
            self._workers[run_id] = worker
            self.registry.update_run(run_id, status="running")
            worker.start()

    def _execute(self, run_id: str, input_value: Any) -> None:
        record = self.registry.get_run(run_id)
        try:
            graph, db, graph_config = self._open_graph(record)
            try:
                graph.invoke(input_value, config=graph_config)
                snapshot = self._snapshot_from_graph(graph, graph_config)
            finally:
                db.close()
            interrupt_payload = snapshot.get("interrupt")
            if interrupt_payload:
                status = (
                    "paused"
                    if interrupt_payload.get("kind") == "controlled_pause"
                    else "interrupted"
                )
                finished = False
            elif snapshot.get("next"):
                status = "recoverable"
                finished = False
            else:
                status = snapshot.get("values", {}).get("status", "completed")
                finished = True
            task = snapshot.get("values", {}).get("task")
            task_id = task.get("id") if isinstance(task, dict) else None
            node = snapshot["next"][0] if snapshot.get("next") else "done"
            self.registry.update_run(
                run_id,
                status=status,
                node=node,
                active_task_id=task_id,
                finished=finished,
            )
        except Exception as exc:
            self.registry.update_run(run_id, status="failed", error=str(exc), finished=True)
        finally:
            with self._lock:
                self._workers.pop(run_id, None)

    def _snapshot(self, record: dict[str, Any]) -> dict[str, Any]:
        try:
            graph, db, graph_config = self._open_graph(record)
            try:
                return self._snapshot_from_graph(graph, graph_config)
            finally:
                db.close()
        except (sqlite3.DatabaseError, KeyError, ValueError):
            return {"values": {}, "next": [], "interrupt": None}

    def _open_graph(self, record: dict[str, Any]):
        project = self.registry.get_project(record["project_id"])
        cfg = load_config(project["config_path"])
        db = sqlite3.connect(cfg.state_dir / "langgraph.sqlite", check_same_thread=False)
        graph = build_graph(checkpointer=SqliteSaver(db))
        graph_config = {"configurable": {"thread_id": record["thread_id"]}}
        return graph, db, graph_config

    @staticmethod
    def _snapshot_from_graph(graph: Any, graph_config: dict[str, Any]) -> dict[str, Any]:
        snapshot = graph.get_state(graph_config)
        return {
            "values": dict(getattr(snapshot, "values", {}) or {}),
            "next": list(getattr(snapshot, "next", ()) or ()),
            "interrupt": _interrupt_payload(snapshot),
        }
