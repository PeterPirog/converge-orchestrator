from __future__ import annotations

import json
import threading
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import uuid4

from langgraph.types import Command

from .budget import (
    RunBudgetExceeded,
    RunBudgetIntegrityError,
    assert_run_wall_time,
    bind_run_id,
    budget_path,
    initialize_run_budget,
    reset_run_id,
)
from .config import (
    load_config,
    load_run_config_snapshot,
    materialize_run_config_snapshot,
)
from .control import ControlSignals
from .evidence import EvidenceStore
from .graph import build_graph
from .model_usage import summarize_model_usage
from .persistence import PersistenceBackend
from .workflow import bootstrap
from .workspace_identity import (
    WorkspaceAffinityError,
    assert_state_store_affinity,
    assert_workspace_affinity,
    state_store_id,
    workspace_id,
)

_TERMINAL_STATUSES = {
    "completed",
    "converged",
    "failed",
    "budget_exhausted",
    "no_changes",
    "spec_changed",
    "stopped",
    "ci_pass",
}
_ACTIVE_STATUSES = {
    "queued",
    "running",
    "pause_requested",
    "paused",
    "interrupted",
    "recoverable",
}
_LEASE_TTL_SECONDS = 120
_LEASE_HEARTBEAT_SECONDS = 30.0


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


def _lease_expiry_is_future(raw: Any) -> bool:
    if not isinstance(raw, str) or not raw:
        return False
    try:
        expiry = datetime.fromisoformat(raw)
    except ValueError:
        # A malformed durable lease must never be interpreted as permission to run concurrently.
        return True
    if expiry.tzinfo is None:
        expiry = expiry.replace(tzinfo=UTC)
    return expiry.astimezone(UTC) > datetime.now(UTC)


class RunController:
    """Coordinates API requests with durable LangGraph checkpoints and run metadata."""

    def __init__(
        self,
        registry_path: Path,
        database_url: str | None = None,
    ):
        self.persistence = PersistenceBackend(registry_path, database_url=database_url)
        self.registry = self.persistence.registry
        self._workers: dict[str, threading.Thread] = {}
        self._lock = threading.Lock()
        self._lease_owner = f"controller-{uuid4().hex}"

    def register_project(self, project_id: str, config_path: Path) -> dict[str, Any]:
        cfg = load_config(config_path)
        if not cfg.repo_path.is_dir() or not (cfg.repo_path / ".git").exists():
            raise ValueError(f"Not a git repository: {cfg.repo_path}")
        if not cfg.requirements_path.is_file():
            raise ValueError(f"Requirements file not found: {cfg.requirements_path}")
        local_workspace_id = workspace_id(cfg.repo_path)
        local_state_store_id = state_store_id(cfg.state_dir)
        return self.registry.register_project(
            project_id,
            config_path,
            workspace_id=local_workspace_id,
            state_store_id=local_state_store_id,
        )

    def _config_for_project(self, project: dict[str, Any]):
        cfg = load_config(project["config_path"])
        assert_workspace_affinity(project, cfg.repo_path)
        assert_state_store_affinity(project, cfg.state_dir)
        return cfg

    def _config_for_run(self, record: dict[str, Any]):
        project = self.registry.get_project(record["project_id"])
        snapshot_path = record.get("config_snapshot_path")
        snapshot_sha256 = record.get("config_snapshot_sha256")
        if bool(snapshot_path) != bool(snapshot_sha256):
            raise RuntimeError(
                f"Run {record['id']} has incomplete pinned configuration metadata"
            )
        if snapshot_path and snapshot_sha256:
            cfg = load_run_config_snapshot(snapshot_path, str(snapshot_sha256))
            assert_workspace_affinity(project, cfg.repo_path)
            assert_state_store_affinity(project, cfg.state_dir)
            return cfg
        # Legacy rows created before per-run config pinning retain their historical behavior.
        return self._config_for_project(project)

    def _config_path_for_run(self, record: dict[str, Any]) -> str:
        snapshot_path = record.get("config_snapshot_path")
        snapshot_sha256 = record.get("config_snapshot_sha256")
        if bool(snapshot_path) != bool(snapshot_sha256):
            raise RuntimeError(
                f"Run {record['id']} has incomplete pinned configuration metadata"
            )
        if snapshot_path:
            return str(Path(str(snapshot_path)).expanduser().resolve())
        project = self.registry.get_project(record["project_id"])
        return str(project["config_path"])

    def _local_project(self, project_id: str) -> tuple[dict[str, Any], Any]:
        project = self.registry.get_project(project_id)
        return project, self._config_for_project(project)

    def _project_is_local(self, project: dict[str, Any]) -> bool:
        try:
            self._config_for_project(project)
        except (WorkspaceAffinityError, FileNotFoundError, OSError, ValueError):
            return False
        return True

    def _foreign_lease_active(self, record: dict[str, Any]) -> bool:
        owner = record.get("lease_owner")
        return bool(
            owner
            and owner != self._lease_owner
            and _lease_expiry_is_future(record.get("lease_expires_at"))
        )

    def bootstrap_project(self, project_id: str) -> dict[str, Any]:
        project, _ = self._local_project(project_id)
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

    def start_run(
        self,
        project_id: str,
        *,
        thread_id: str | None = None,
    ) -> dict[str, Any]:
        project = self.registry.get_project(project_id)
        self._config_for_project(project)
        for existing in self.registry.runs_for_project(project_id):
            if existing["status"] in _ACTIVE_STATUSES and not existing["finished_at"]:
                raise RuntimeError(f"Project already has active run {existing['id']}")
        run_id = uuid4().hex
        resolved_thread_id = thread_id or f"run-{run_id}"
        if not resolved_thread_id.strip():
            raise ValueError("thread_id must not be empty")

        snapshot_path: Path | None = None
        try:
            cfg, snapshot_path, snapshot_sha256 = materialize_run_config_snapshot(
                project["config_path"],
                run_id,
            )
            assert_workspace_affinity(project, cfg.repo_path)
            assert_state_store_affinity(project, cfg.state_dir)
            record = self.registry.create_run(
                run_id,
                project_id,
                resolved_thread_id,
                config_snapshot_path=snapshot_path,
                config_snapshot_sha256=snapshot_sha256,
            )
        except Exception:
            if snapshot_path is not None:
                snapshot_path.unlink(missing_ok=True)
            raise

        initial: dict[str, Any] = {
            "project_id": project_id,
            "config_path": str(snapshot_path),
            "run_id": run_id,
            "thread_id": resolved_thread_id,
        }
        self._submit(run_id, initial)
        return record

    def request_pause(self, run_id: str) -> dict[str, Any]:
        record = self.registry.get_run(run_id)
        if record["finished_at"]:
            raise RuntimeError("Cannot pause a finished run")
        cfg = self._config_for_run(record)
        ControlSignals(cfg.state_dir).request_pause(run_id)
        self.registry.update_run(run_id, status="pause_requested")
        return self.status(run_id)

    def resume(self, run_id: str) -> dict[str, Any]:
        record = self.registry.get_run(run_id)
        cfg = self._config_for_run(record)
        snapshot = self._snapshot(record)
        interrupt_payload = snapshot.get("interrupt")
        if interrupt_payload and interrupt_payload.get("kind") != "controlled_pause":
            raise RuntimeError("Run requires /decision, not /resume")
        if interrupt_payload:
            ControlSignals(cfg.state_dir).clear_pause(run_id)
            self._submit(run_id, Command(resume="resume"))
        elif snapshot.get("next"):
            self._submit(run_id, None)
        else:
            raise RuntimeError("Run has no resumable checkpoint")
        return self.registry.get_run(run_id)

    def decide(self, run_id: str, decision: dict[str, Any]) -> dict[str, Any]:
        record = self.registry.get_run(run_id)
        self._config_for_run(record)
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
        self._config_for_run(record)
        snapshot = self._snapshot(record)
        with self._lock:
            worker_alive = bool(
                self._workers.get(run_id) and self._workers[run_id].is_alive()
            )
        remote_worker_active = self._foreign_lease_active(record)
        status = record["status"]
        if snapshot.get("interrupt"):
            kind = snapshot["interrupt"].get("kind")
            status = "paused" if kind == "controlled_pause" else "interrupted"
        elif (
            snapshot.get("next")
            and not worker_alive
            and not remote_worker_active
            and status in {"queued", "running"}
        ):
            status = "recoverable"
        elif (
            snapshot.get("values")
            and not snapshot.get("next")
            and not worker_alive
            and not remote_worker_active
        ):
            status = snapshot["values"].get("status", status)
        task = snapshot.get("values", {}).get("task")
        active_task_id = task.get("id") if isinstance(task, dict) else None
        node = snapshot["next"][0] if snapshot.get("next") else None
        self.registry.update_run(
            run_id,
            status=status,
            node=node or record.get("node") or "done",
            active_task_id=active_task_id,
        )
        result = self.registry.get_run(run_id)
        result.update(snapshot)
        result["worker_alive"] = worker_alive
        result["remote_worker_active"] = remote_worker_active
        return result

    def model_usage(self, run_id: str) -> dict[str, Any]:
        record = self.registry.get_run(run_id)
        cfg = self._config_for_run(record)
        return summarize_model_usage(cfg, run_id).model_dump(mode="json")

    def interrupt(self, run_id: str) -> dict[str, Any] | None:
        record = self.registry.get_run(run_id)
        self._config_for_run(record)
        return self._snapshot(record).get("interrupt")

    def compliance(self, project_id: str) -> dict[str, Any]:
        _, cfg = self._local_project(project_id)
        path = cfg.state_dir / "compliance.json"
        if not path.is_file():
            raise FileNotFoundError(path)
        return json.loads(path.read_text(encoding="utf-8"))

    def evidence(self, task_id: str) -> list[dict[str, Any]]:
        matches: list[dict[str, Any]] = []
        for project in self.registry.list_projects():
            if not self._project_is_local(project):
                continue
            cfg = load_config(project["config_path"])
            store = EvidenceStore(cfg.state_dir / "evidence")
            for bundle in store.find_task_bundles(task_id):
                matches.append({"project_id": project["id"], **bundle})
        return matches

    def _enforce_submission_budget(self, record: dict[str, Any]) -> None:
        cfg = self._config_for_run(record)
        path = budget_path(cfg, record["id"])
        if not path.exists():
            if record.get("status") != "queued" or record.get("finished_at"):
                raise RunBudgetIntegrityError(
                    f"run budget ledger missing for active run {record['id']}"
                )
            initialize_run_budget(
                cfg,
                record["id"],
                started_at=str(record["started_at"]),
            )
        assert_run_wall_time(cfg, record["id"])

    def _submit(self, run_id: str, input_value: Any) -> None:
        with self._lock:
            existing = self._workers.get(run_id)
            if existing and existing.is_alive():
                raise RuntimeError("Run is already executing")
            if not self.registry.claim_run_lease(
                run_id,
                self._lease_owner,
                _LEASE_TTL_SECONDS,
            ):
                raise RuntimeError("Run is leased by another active controller")
            try:
                record = self.registry.get_run(run_id)
                self._enforce_submission_budget(record)
            except RunBudgetExceeded as exc:
                self.registry.update_run(
                    run_id,
                    status="budget_exhausted",
                    node="done",
                    error=str(exc),
                    finished=True,
                )
                self.registry.release_run_lease(run_id, self._lease_owner)
                return
            except RunBudgetIntegrityError as exc:
                self.registry.update_run(
                    run_id,
                    status="failed",
                    node="done",
                    error=str(exc),
                    finished=True,
                )
                self.registry.release_run_lease(run_id, self._lease_owner)
                return
            try:
                worker = threading.Thread(
                    target=self._execute,
                    args=(run_id, input_value),
                    name=f"converge-{run_id[:8]}",
                    daemon=True,
                )
                self._workers[run_id] = worker
                self.registry.update_run(run_id, status="running")
                worker.start()
            except Exception:
                self._workers.pop(run_id, None)
                self.registry.release_run_lease(run_id, self._lease_owner)
                raise

    def _lease_heartbeat(self, run_id: str, stop: threading.Event) -> None:
        while not stop.wait(_LEASE_HEARTBEAT_SECONDS):
            try:
                renewed = self.registry.renew_run_lease(
                    run_id,
                    self._lease_owner,
                    _LEASE_TTL_SECONDS,
                )
            except Exception as exc:
                if self.persistence.is_database_error(exc):
                    continue
                raise
            if not renewed:
                return

    def _execute(self, run_id: str, input_value: Any) -> None:
        record = self.registry.get_run(run_id)
        lease_stop = threading.Event()
        heartbeat = threading.Thread(
            target=self._lease_heartbeat,
            args=(run_id, lease_stop),
            name=f"converge-lease-{run_id[:8]}",
            daemon=True,
        )
        heartbeat.start()
        run_token = bind_run_id(run_id)
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
        except RunBudgetExceeded as exc:
            self.registry.update_run(
                run_id,
                status="budget_exhausted",
                node="done",
                error=str(exc),
                finished=True,
            )
        except RunBudgetIntegrityError as exc:
            self.registry.update_run(
                run_id,
                status="failed",
                node="done",
                error=str(exc),
                finished=True,
            )
        except Exception as exc:
            self.registry.update_run(run_id, status="failed", error=str(exc), finished=True)
        finally:
            reset_run_id(run_token)
            lease_stop.set()
            heartbeat.join(timeout=1)
            self.registry.release_run_lease(run_id, self._lease_owner)
            with self._lock:
                self._workers.pop(run_id, None)

    def _snapshot(self, record: dict[str, Any]) -> dict[str, Any]:
        try:
            graph, db, graph_config = self._open_graph(record)
            try:
                return self._snapshot_from_graph(graph, graph_config)
            finally:
                db.close()
        except (KeyError, ValueError):
            return {"values": {}, "next": [], "interrupt": None}
        except Exception as exc:
            if self.persistence.is_database_error(exc):
                return {"values": {}, "next": [], "interrupt": None}
            raise

    def _open_graph(self, record: dict[str, Any]):
        cfg = self._config_for_run(record)
        checkpointer, db = self.persistence.open_checkpointer(cfg.state_dir)
        graph = build_graph(checkpointer=checkpointer)
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
