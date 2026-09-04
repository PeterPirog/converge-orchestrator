from __future__ import annotations

import threading
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from langgraph.types import Command

from .config import load_config
from .graph_service import build_graph
from .remote import RemoteValidationError, validate_origin_repository
from .runtime import _TERMINAL_STATUSES, RunController

_CONTENTION_RETRY_SECONDS = 5
_AUTO_RECOVERY_DELAY_SECONDS = 0.05
_CHECKPOINT_INSPECTION_RETRY_SECONDS = 1.0
_PRECHECKPOINT_RECOVERY_STATUSES = {
    "queued",
    "running",
    "pause_requested",
    "recoverable",
}
_INITIAL_INPUT_KEYS = {"project_id", "config_path", "run_id", "thread_id"}


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


def _terminal_checkpoint_status(snapshot: dict[str, Any]) -> str | None:
    if snapshot.get("interrupt") or snapshot.get("next"):
        return None
    values = snapshot.get("values")
    if not isinstance(values, dict) or not values:
        return None
    status = values.get("status")
    return str(status) if status in _TERMINAL_STATUSES else None


def _is_initial_input_checkpoint(
    snapshot: dict[str, Any],
    record: dict[str, Any],
) -> bool:
    """Recognize only the exact durable input envelope written before the first graph node."""
    if snapshot.get("interrupt") or snapshot.get("next"):
        return False
    values = snapshot.get("values")
    if not isinstance(values, dict) or set(values) != _INITIAL_INPUT_KEYS:
        return False
    return (
        values.get("project_id") == record.get("project_id")
        and values.get("run_id") == record.get("id")
        and values.get("thread_id") == record.get("thread_id")
        and isinstance(values.get("config_path"), str)
        and bool(values.get("config_path"))
    )


class ScheduledRunController(RunController):
    """Durable LangGraph controller with machine-managed wait and crash recovery."""

    def __init__(
        self,
        registry_path: Path,
        database_url: str | None = None,
        *,
        restore_on_start: bool = True,
    ):
        super().__init__(registry_path, database_url=database_url)
        self._timers: dict[str, threading.Timer] = {}
        self._timer_generations: dict[str, int] = {}
        if restore_on_start:
            self.restore_durable_runs()

    def restore_durable_runs(self, project_id: str | None = None) -> None:
        """Restore all local durable work or only one explicitly selected project."""
        self._restore_ci_waits(project_id)
        self._restore_recoverable_runs(project_id)

    def register_project(self, project_id: str, config_path: Path) -> dict[str, Any]:
        cfg = load_config(config_path)
        if cfg.github_repo:
            try:
                validate_origin_repository(cfg.repo_path, cfg.github_repo)
            except RemoteValidationError as exc:
                raise ValueError(str(exc)) from exc
        try:
            previous = self.registry.get_project(project_id)
        except KeyError:
            previous = None
        needs_rebind_recovery = bool(
            previous
            and (
                not previous.get("workspace_id")
                or not previous.get("state_store_id")
            )
        )
        result = super().register_project(project_id, config_path)
        if needs_rebind_recovery:
            # Legacy rows were deliberately not auto-recovered before their physical bindings were
            # known. Explicit re-registration is the one safe point to retry their durable work.
            self.restore_durable_runs(project_id)
        return result

    def start_run(
        self,
        project_id: str,
        *,
        thread_id: str | None = None,
    ) -> dict[str, Any]:
        for existing in self.registry.runs_for_project(project_id):
            if existing["status"] == "waiting_ci" and not existing["finished_at"]:
                raise RuntimeError(f"Project already has active run {existing['id']}")
        return super().start_run(project_id, thread_id=thread_id)

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
        remote_worker_active = bool(result.get("remote_worker_active"))
        worker_alive = bool(result.get("worker_alive"))
        terminal_status = _terminal_checkpoint_status(result)
        if (
            terminal_status is not None
            and not worker_alive
            and not remote_worker_active
            and not result.get("finished_at")
        ):
            # The graph may have durably completed immediately before the process lost
            # the registry update. Later status reconciliation closes that crash window
            # without rerunning.
            self.registry.update_run(
                run_id,
                status=terminal_status,
                node="done",
                finished=True,
            )
            self._cancel_timer(run_id)
            persisted = self.registry.get_run(run_id)
            for key in (
                "values",
                "next",
                "interrupt",
                "worker_alive",
                "remote_worker_active",
            ):
                persisted[key] = result.get(key)
            return persisted
        if interrupt_payload and interrupt_payload.get("kind") == "ci_wait":
            self.registry.update_run(run_id, status="waiting_ci", node="ci_wait")
            result["status"] = "waiting_ci"
            if not worker_alive and not remote_worker_active:
                self._schedule_ci_wait(run_id, str(interrupt_payload["wake_at"]))
        elif (
            not interrupt_payload
            and result.get("next")
            and not worker_alive
            and not remote_worker_active
            and not result.get("finished_at")
        ):
            self.registry.update_run(
                run_id,
                status="recoverable",
                node=str(result["next"][0]),
            )
            result["status"] = "recoverable"
            self._schedule_recoverable(run_id)
        elif (
            not worker_alive
            and not remote_worker_active
            and not result.get("finished_at")
            and result.get("status") in _PRECHECKPOINT_RECOVERY_STATUSES
            and _is_initial_input_checkpoint(result, result)
        ):
            self.registry.update_run(run_id, status="recoverable", node="start")
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
        """Open the canonical service graph only for this controller's bound workspace."""
        _, cfg = self._local_project(record["project_id"])
        checkpointer, db = self.persistence.open_checkpointer(cfg.state_dir)
        graph = build_graph(checkpointer=checkpointer)
        graph_config = {"configurable": {"thread_id": record["thread_id"]}}
        return graph, db, graph_config

    def _recovery_snapshot(self, record: dict[str, Any]) -> dict[str, Any] | None:
        """Read restart state and retry only backend-classified transient failures."""
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
            if self.persistence.is_transient_error(exc):
                self._schedule_recoverable(
                    record["id"],
                    _CHECKPOINT_INSPECTION_RETRY_SECONDS,
                )
            return None

    def _initial_recovery_input(self, record: dict[str, Any]) -> dict[str, Any]:
        project, _ = self._local_project(record["project_id"])
        return {
            "project_id": record["project_id"],
            "config_path": project["config_path"],
            "run_id": record["id"],
            "thread_id": record["thread_id"],
        }

    def _unfinished_records(self, project_id: str | None = None):
        if project_id is None:
            projects = self.registry.list_projects()
        else:
            try:
                projects = [self.registry.get_project(project_id)]
            except KeyError:
                projects = []
        for project in projects:
            if not self._project_is_local(project):
                continue
            for record in self.registry.runs_for_project(project["id"]):
                if not record["finished_at"]:
                    yield record

    def _restore_ci_waits(self, project_id: str | None = None) -> None:
        for record in self._unfinished_records(project_id):
            if self._foreign_lease_active(record):
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

    def _restore_recoverable_runs(self, project_id: str | None = None) -> None:
        """Reconcile terminal state or resume durable machine work after restart."""
        for record in self._unfinished_records(project_id):
            if self._foreign_lease_active(record):
                continue
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
                record.get("status") in _PRECHECKPOINT_RECOVERY_STATUSES
                and (
                    not snapshot.get("values")
                    or _is_initial_input_checkpoint(snapshot, record)
                )
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
        if self._foreign_lease_active(record):
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
        if record["finished_at"] or self._foreign_lease_active(record):
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
            record.get("status") in _PRECHECKPOINT_RECOVERY_STATUSES
            and (
                not snapshot.get("values")
                or _is_initial_input_checkpoint(snapshot, record)
            )
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
