from __future__ import annotations

import sqlite3
import threading
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from langgraph.checkpoint.sqlite import SqliteSaver
from langgraph.types import Command

from .config import load_config
from .graph_service import build_graph
from .runtime import RunController

_CONTENTION_RETRY_SECONDS = 5


def _wake_datetime(value: str) -> datetime:
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


class ScheduledRunController(RunController):
    """RunController that treats `ci_wait` as a durable machine interrupt."""

    def __init__(self, registry_path: Path):
        super().__init__(registry_path)
        self._timers: dict[str, threading.Timer] = {}
        self._timer_generations: dict[str, int] = {}
        self._restore_ci_waits()

    def start_run(self, project_id: str) -> dict[str, Any]:
        for existing in self.registry.runs_for_project(project_id):
            if existing["status"] == "waiting_ci" and not existing["finished_at"]:
                raise RuntimeError(f"Project already has active run {existing['id']}")
        return super().start_run(project_id)

    def resume(self, run_id: str) -> dict[str, Any]:
        record = self.registry.get_run(run_id)
        snapshot = self._snapshot(record)
        interrupt_payload = snapshot.get("interrupt")
        if interrupt_payload and interrupt_payload.get("kind") == "ci_wait":
            try:
                self._submit(run_id, Command(resume="resume"))
            except Exception:
                self._schedule_ci_wait(run_id, str(interrupt_payload["wake_at"]))
                raise
            self._cancel_timer(run_id)
            return self.registry.get_run(run_id)
        return super().resume(run_id)

    def decide(self, run_id: str, decision: dict[str, Any]) -> dict[str, Any]:
        record = self.registry.get_run(run_id)
        snapshot = self._snapshot(record)
        interrupt_payload = snapshot.get("interrupt")
        if interrupt_payload and interrupt_payload.get("kind") == "ci_wait":
            raise RuntimeError(
                "CI wait is machine-managed; use resume only for an early poll"
            )
        return super().decide(run_id, decision)

    def status(self, run_id: str) -> dict[str, Any]:
        result = super().status(run_id)
        interrupt_payload = result.get("interrupt")
        if interrupt_payload and interrupt_payload.get("kind") == "ci_wait":
            self.registry.update_run(run_id, status="waiting_ci", node="ci_wait")
            result["status"] = "waiting_ci"
            self._schedule_ci_wait(run_id, str(interrupt_payload["wake_at"]))
        return result

    def _execute(self, run_id: str, input_value: Any) -> None:
        super()._execute(run_id, input_value)
        try:
            record = self.registry.get_run(run_id)
        except KeyError:
            return
        if record["finished_at"]:
            self._cancel_timer(run_id)
            return
        snapshot = self._snapshot(record)
        interrupt_payload = snapshot.get("interrupt")
        if interrupt_payload and interrupt_payload.get("kind") == "ci_wait":
            self.registry.update_run(run_id, status="waiting_ci", node="ci_wait")
            self._schedule_ci_wait(run_id, str(interrupt_payload["wake_at"]))
        else:
            self._cancel_timer(run_id)

    def _open_graph(self, record: dict[str, Any]):
        project = self.registry.get_project(record["project_id"])
        cfg = load_config(project["config_path"])
        db = sqlite3.connect(
            cfg.state_dir / "langgraph.sqlite",
            check_same_thread=False,
        )
        graph = build_graph(checkpointer=SqliteSaver(db))
        graph_config = {"configurable": {"thread_id": record["thread_id"]}}
        return graph, db, graph_config

    def _restore_ci_waits(self) -> None:
        for project in self.registry.list_projects():
            for record in self.registry.runs_for_project(project["id"]):
                if record["finished_at"]:
                    continue
                snapshot = self._snapshot(record)
                interrupt_payload = snapshot.get("interrupt")
                if interrupt_payload and interrupt_payload.get("kind") == "ci_wait":
                    self.registry.update_run(
                        record["id"],
                        status="waiting_ci",
                        node="ci_wait",
                    )
                    self._schedule_ci_wait(
                        record["id"],
                        str(interrupt_payload["wake_at"]),
                    )

    def _schedule_ci_wait(self, run_id: str, wake_at: str) -> None:
        wake = _wake_datetime(wake_at)
        delay = max(0.0, (wake - datetime.now(UTC)).total_seconds())
        with self._lock:
            existing = self._timers.get(run_id)
            if existing is not None:
                existing.cancel()
            generation = self._timer_generations.get(run_id, 0) + 1
            self._timer_generations[run_id] = generation
            timer = threading.Timer(
                delay,
                self._resume_ci_wait,
                args=(run_id, generation),
            )
            timer.daemon = True
            self._timers[run_id] = timer
            timer.start()

    def _cancel_timer(self, run_id: str) -> None:
        with self._lock:
            timer = self._timers.pop(run_id, None)
            self._timer_generations[run_id] = (
                self._timer_generations.get(run_id, 0) + 1
            )
        if timer is not None:
            timer.cancel()

    def _resume_ci_wait(self, run_id: str, generation: int) -> None:
        with self._lock:
            if self._timer_generations.get(run_id) != generation:
                return
            self._timers.pop(run_id, None)
        try:
            record = self.registry.get_run(run_id)
        except KeyError:
            return
        if record["finished_at"]:
            return

        snapshot = self._snapshot(record)
        interrupt_payload = snapshot.get("interrupt")
        if not interrupt_payload or interrupt_payload.get("kind") != "ci_wait":
            return
        try:
            self._submit(run_id, Command(resume="resume"))
        except RuntimeError as exc:
            message = str(exc)
            lease_conflict = "leased by another active controller" in message
            local_conflict = "already executing" in message
            if not lease_conflict and not local_conflict:
                raise
            retry_at = datetime.now(UTC) + timedelta(
                seconds=_CONTENTION_RETRY_SECONDS
            )
            self._schedule_ci_wait(run_id, retry_at.isoformat())
