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
from .remote import RemoteValidationError, validate_origin_repository
from .runtime import RunController, _TERMINAL_STATUSES

_CONTENTION_RETRY_SECONDS = 5
_AUTO_RECOVERY_DELAY_SECONDS = 0.05
_CHECKPOINT_INSPECTION_RETRY_SECONDS = 1.0
_PRECHECKPOINT_RECOVERY_STATUSES = {
    "queued",
    "running",
    "pause_requested",
    "recoverable",
}


def _wake_datetime(value: str) -> datetime:
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def _is_contention_error(exc: RuntimeError) -> bool:
    message = str(exc)
    return (
        "leased by another active controller" in message
        or "already executing" in message
    )


def _is_transient_checkpoint_error(exc: Exception) -> bool:
    if not isinstance(exc, sqlite3.OperationalError):
        return False
    message = str(exc).lower()
    return "locked" in message or "busy" in message


def _terminal_checkpoint_status(snapshot: dict[str, Any]) -> str | None:
    if snapshot.get("interrupt") or snapshot.get("next"):
        return None
    values = snapshot.get("values")
    if not isinstance(values, dict) or not values:
        return None
    status = values.get("status")
    return str(status) if status in _TERMINAL_STATUSES else None


class ScheduledRunController(RunController):
    """Durable LangGraph controller with machine-managed wait and crash recovery."""

    def __init__(self, registry_path: Path):
        super().__init__(registry_path)
        self._timers: dict[str, threading.Timer] = {}
        self._timer_generations: dict[str, int] = {}
        self._restore_ci_waits()
        self._restore_recoverable_runs()

    def register_project(self, project_id: str, config_path: Path) -> dict[str, Any]:
        cfg = load_config(config_path)
        if cfg.github_repo:
            try:
                validate_origin_repository(cfg.repo_path, cfg.github_repo)
            except RemoteValidationError as exc:
                raise ValueError(str(exc)) from exc
        return super().register_project(project_id, config_path)

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

        result = super().resume(run_id)
        self._cancel_timer(run_id)
        return result

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
            if not result.get("worker_alive"):
                self._schedule_ci_wait(run_id, str(interrupt_payload["wake_at"]))
        elif (
            not interrupt_payload
            and result.get("next")
            and not result.get("worker_alive")
            and not result.get("finished_at")
        ):
            self.registry.update_run(
                run_id,
                status="recoverable",
                node=str(result["next"][0]),
            )
            result["status"] = "recoverable"
            self._schedule_recoverable(run_id)
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
        elif not interrupt_payload and snapshot.get("next"):
            self.registry.update_run(
                run_id,
                status="recoverable",
                node=str(snapshot["next"][0]),
            )
            self._schedule_recoverable(run_id)
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

    def _recovery_snapshot(self, record: dict[str, Any]) -> dict[str, Any] | None:
        """Read restart state, retrying only transient SQLite lock/busy failures."""
        try:
            graph, db, graph_config = self._open_graph(record)
            try:
                return self._snapshot_from_graph(graph, graph_config)
            finally:
                db.close()
        except Exception as exc:
            self.registry.update_run(
                record["id"],
                error=(
                    "automatic checkpoint inspection failed: "
                    f"{type(exc).__name__}: {exc}"
                ),
            )
            if _is_transient_checkpoint_error(exc):
                self._schedule_recoverable(
                    record["id"],
                    _CHECKPOINT_INSPECTION_RETRY_SECONDS,
                )
            return None

    def _initial_recovery_input(self, record: dict[str, Any]) -> dict[str, Any]:
        project = self.registry.get_project(record["project_id"])
        return {
            "project_id": record["project_id"],
            "config_path": project["config_path"],
            "run_id": record["id"],
            "thread_id": record["thread_id"],
        }

    def _unfinished_records(self):
        for project in self.registry.list_projects():
            for record in self.registry.runs_for_project(project["id"]):
                if not record["finished_at"]:
                    yield record

    def _restore_ci_waits(self) -> None:
        for record in self._unfinished_records():
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

    def _restore_recoverable_runs(self) -> None:
        """Reconcile terminal state or resume durable machine work after restart."""
        for record in self._unfinished_records():
            snapshot = self._recovery_snapshot(record)
            if snapshot is None or snapshot.get("interrupt"):
                continue
            terminal_status = _terminal_checkpoint_status(snapshot)
            if terminal_status is not None:
                self.registry.update_run(
                    record["id"],
                    status=terminal_status,
                    node="done",
                    finished=True,
                )
                self._cancel_timer(record["id"])
                continue
            if snapshot.get("next"):
                node = str(snapshot["next"][0])
            elif (
                not snapshot.get("values")
                and record.get("status") in _PRECHECKPOINT_RECOVERY_STATUSES
            ):
                node = "start"
            else:
                continue
            self.registry.update_run(
                record["id"],
                status="recoverable",
                node=node,
            )
            self._schedule_recoverable(record["id"])

    def _schedule_ci_wait(self, run_id: str, wake_at: str) -> None:
        wake = _wake_datetime(wake_at)
        delay = max(0.0, (wake - datetime.now(UTC)).total_seconds())
        self._replace_timer(run_id, delay, self._resume_ci_wait)

    def _schedule_recoverable(
        self,
        run_id: str,
        delay: float = _AUTO_RECOVERY_DELAY_SECONDS,
    ) -> None:
        self._replace_timer(run_id, max(0.0, delay), self._resume_recoverable)

    def _replace_timer(
        self,
        run_id: str,
        delay: float,
        callback: Any,
    ) -> None:
        with self._lock:
            existing = self._timers.get(run_id)
            if existing is not None:
                existing.cancel()
            generation = self._timer_generations.get(run_id, 0) + 1
            self._timer_generations[run_id] = generation
            timer = threading.Timer(delay, callback, args=(run_id, generation))
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

    def _take_timer_generation(self, run_id: str, generation: int) -> bool:
        with self._lock:
            if self._timer_generations.get(run_id) != generation:
                return False
            self._timers.pop(run_id, None)
            return True

    def _retry_after_contention(self, run_id: str, *, ci_wait: bool) -> None:
        if ci_wait:
            retry_at = datetime.now(UTC) + timedelta(seconds=_CONTENTION_RETRY_SECONDS)
            self._schedule_ci_wait(run_id, retry_at.isoformat())
        else:
            self._schedule_recoverable(run_id, float(_CONTENTION_RETRY_SECONDS))

    def _resume_ci_wait(self, run_id: str, generation: int) -> None:
        if not self._take_timer_generation(run_id, generation):
            return
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
            if not _is_contention_error(exc):
                self.registry.update_run(
                    run_id,
                    status="waiting_ci",
                    error=f"automatic CI resume failed: {exc}",
                )
                return
            self._retry_after_contention(run_id, ci_wait=True)

    def _resume_recoverable(self, run_id: str, generation: int) -> None:
        if not self._take_timer_generation(run_id, generation):
            return
        try:
            record = self.registry.get_run(run_id)
        except KeyError:
            return
        if record["finished_at"]:
            return

        snapshot = self._recovery_snapshot(record)
        if snapshot is None or snapshot.get("interrupt"):
            return
        terminal_status = _terminal_checkpoint_status(snapshot)
        if terminal_status is not None:
            self.registry.update_run(
                run_id,
                status=terminal_status,
                node="done",
                finished=True,
            )
            return
        if snapshot.get("next"):
            input_value: Any = None
        elif (
            not snapshot.get("values")
            and record.get("status") in _PRECHECKPOINT_RECOVERY_STATUSES
        ):
            input_value = self._initial_recovery_input(record)
        else:
            return
        try:
            self._submit(run_id, input_value)
        except RuntimeError as exc:
            if not _is_contention_error(exc):
                self.registry.update_run(
                    run_id,
                    status="recoverable",
                    error=f"automatic checkpoint resume failed: {exc}",
                )
                return
            self._retry_after_contention(run_id, ci_wait=False)
