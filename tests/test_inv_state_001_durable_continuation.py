"""Lock INV-STATE-001: workflow continuation depends on durable state and explicit evidence.

INV-STATE-001 (CONVERGE_ORCHESTRATOR_2_0_SOURCE_OF_TRUTH.md:L152 [4. Global invariants]):
Workflow continuation SHALL depend on durable state and explicit evidence rather than
chat history, model-session history, process memory, or human recollection.

These deterministic offline tests pin the unchanged runtime's durable-continuation contract:
a freshly constructed ScheduledRunController (empty worker map) reconstructs the
continuation point solely from the durable SQLite LangGraph checkpoint and control registry;
recovery of a pending durable thread calls _submit with input None (checkpoint replay); a
pre-checkpoint "queued" run replays the exact durable initial-input envelope from the
registry record and pinned configuration; and EvidenceStore events written by one instance
are read back intact by a brand-new independent instance over the same state directory.
No network, model, provider, or git-remote access; the graph is a local stand-in and no
worker process is spawned.
"""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any, TypedDict

import pytest
from langgraph.graph import END, START, StateGraph

from converge_orchestrator import runtime_service
from converge_orchestrator.config import materialize_run_config_snapshot
from converge_orchestrator.evidence import EvidenceStore
from converge_orchestrator.persistence import open_checkpointer
from converge_orchestrator.registry import ControlRegistry
from converge_orchestrator.runtime_service import ScheduledRunController

_PROJECT_ID = "state-proj"


class _ProcessDeath(RuntimeError):
    """Simulated death of the controller process inside a workflow node."""


class _State(TypedDict, total=False):
    project_id: str
    config_path: str
    run_id: str
    thread_id: str
    status: str


def _first_node(state: _State) -> dict[str, str]:
    return {"status": "first_done"}


def _dead_node(state: _State) -> dict[str, str]:
    raise _ProcessDeath("simulated process death before durable completion")


def build_death_graph(checkpointer: Any = None):
    """Deterministic offline workflow: one durable step followed by a fatal node."""
    graph = StateGraph(_State)
    graph.add_node("first", _first_node)
    graph.add_node("dead", _dead_node)
    graph.add_edge(START, "first")
    graph.add_edge("first", "dead")
    graph.add_edge("dead", END)
    return graph.compile(checkpointer=checkpointer)


def _local_git_repository(path: Path) -> Path:
    git_dir = path / ".git"
    (git_dir / "objects").mkdir(parents=True)
    (git_dir / "refs").mkdir(parents=True)
    (git_dir / "HEAD").write_text("ref: refs/heads/main\n", encoding="utf-8")
    return path


def _write_config(tmp_path: Path, repo: Path) -> tuple[Path, Path]:
    state_dir = tmp_path / "state"
    worktree_dir = tmp_path / "worktrees"
    requirements = tmp_path / "architecture.md"
    requirements.write_text(
        "Workflow continuation must depend on durable state and explicit evidence.\n",
        encoding="utf-8",
    )
    config_path = tmp_path / "converge.yaml"
    config_path.write_text(
        f"version: 1\nproject:\n"
        f"  repo_path: {json.dumps(str(repo))}\n"
        f"  requirements_path: {json.dumps(str(requirements))}\n"
        f"  state_dir: {json.dumps(str(state_dir))}\n"
        f"  worktree_dir: {json.dumps(str(worktree_dir))}\n"
        "  require_spec_read_only: false\nagents: {}\n",
        encoding="utf-8",
    )
    return config_path, state_dir


@pytest.fixture
def durable_world(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> dict[str, Path]:
    repo = _local_git_repository(tmp_path / "repo")
    config_path, state_dir = _write_config(tmp_path, repo)
    monkeypatch.setattr(runtime_service, "build_graph", build_death_graph)
    return {"config_path": config_path, "state_dir": state_dir,
            "registry_path": state_dir / "control.sqlite"}


def _leave_dead_durable_run(
    world: dict[str, Path], run_id: str, thread_id: str,
    *, write_checkpoint: bool, worker_started: bool,
) -> None:
    """Reproduce what a dead controller leaves behind in durable storage.

    The registry row, pinned configuration, held lease, and (optionally) the resumable
    SQLite checkpoint exist on disk; recovery is asserted on a brand-new controller.
    """
    dead = ScheduledRunController(world["registry_path"], restore_on_start=False)
    dead.register_project(_PROJECT_ID, world["config_path"])
    registry = dead.registry

    # RunController.start_run pins the configuration before the registry row so recovery
    # can later resolve configuration from the registry alone.
    _, snapshot_path, snapshot_sha256 = materialize_run_config_snapshot(
        str(world["config_path"]), run_id
    )
    registry.create_run(
        run_id,
        _PROJECT_ID,
        thread_id,
        config_snapshot_path=snapshot_path,
        config_snapshot_sha256=snapshot_sha256,
    )
    if worker_started:
        # _submit flips the row to running and claims the lease before the worker starts.
        registry.update_run(run_id, status="running")
        registry.claim_run_lease(run_id, "dead-controller", 1)
        if write_checkpoint:
            # The graph durably checkpointed "first"; the process then died inside "dead".
            checkpointer, db = open_checkpointer(world["state_dir"])
            try:
                build_death_graph(checkpointer).invoke(
                    {"project_id": _PROJECT_ID, "config_path": str(snapshot_path),
                     "run_id": run_id, "thread_id": thread_id},
                    config={"configurable": {"thread_id": thread_id}},
                    durability="sync",
                )
            except _ProcessDeath:
                pass  # process death: durable state is intentionally left as-is
            finally:
                db.close()
        # Deterministically release the dead controller's lease (no wall-clock sleep).
        registry.release_run_lease(run_id, "dead-controller")


def _snapshot(state_dir: Path, thread_id: str) -> dict[str, Any]:
    checkpointer, db = open_checkpointer(state_dir)
    try:
        state = build_death_graph(checkpointer).get_state(
            {"configurable": {"thread_id": thread_id}}
        )
        return {"values": dict(state.values), "next": list(state.next)}
    finally:
        db.close()


def _record_submissions(monkeypatch: pytest.MonkeyPatch) -> list[tuple[str, Any]]:
    """Stub the worker-submission boundary and record exactly what recovery submits."""
    submissions: list[tuple[str, Any]] = []

    def _recording_submit(
        self: ScheduledRunController, run_id: str, input_value: Any
    ) -> None:
        submissions.append((run_id, input_value))

    monkeypatch.setattr(ScheduledRunController, "_submit", _recording_submit)
    return submissions


def _wait_for(predicate: Any, timeout: float = 5.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline and not predicate():
        time.sleep(0.01)
    assert predicate(), "durable recovery did not reach the expected state in time"


def test_fresh_controller_recovers_pending_node_from_durable_checkpoint(
    durable_world: dict[str, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    """A new controller reconstructs status/node solely from durable storage."""
    run_id = "run-recoverable"
    thread_id = "thread-recoverable"
    _leave_dead_durable_run(
        durable_world, run_id, thread_id, write_checkpoint=True, worker_started=True
    )

    # Preconditions: checkpoint persisted before construction; run unfinished.
    registry = ControlRegistry(durable_world["registry_path"])
    assert registry.get_run(run_id)["status"] == "running"
    assert _snapshot(durable_world["state_dir"], thread_id)["next"] == ["dead"]

    submissions = _record_submissions(monkeypatch)
    controller = ScheduledRunController(durable_world["registry_path"])

    # Continuation point recovered from durable state alone: this fresh process holds
    # no worker state from the previous process.
    recovered = registry.get_run(run_id)
    assert (recovered["status"], recovered["node"]) == ("recoverable", "dead")
    assert recovered["finished_at"] is None
    assert controller._workers == {}

    # Automatic recovery resumes the durable thread with input None (checkpoint replay).
    _wait_for(lambda: len(submissions) == 1)
    assert submissions == [(run_id, None)]


def test_queued_run_without_checkpoint_replays_durable_initial_input(
    durable_world: dict[str, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    """A pre-checkpoint queued run is replayed from registry/config, not memory."""
    run_id = "run-precheckpoint"
    thread_id = "thread-precheckpoint"
    _leave_dead_durable_run(
        durable_world, run_id, thread_id, write_checkpoint=False, worker_started=False
    )

    # Preconditions: queued, unfinished, and no durable checkpoint for the thread yet.
    registry = ControlRegistry(durable_world["registry_path"])
    assert registry.get_run(run_id)["status"] == "queued"
    snapshot = _snapshot(durable_world["state_dir"], thread_id)
    assert not snapshot["values"] and not snapshot["next"]

    submissions = _record_submissions(monkeypatch)
    controller = ScheduledRunController(durable_world["registry_path"])

    recovered = registry.get_run(run_id)
    assert (recovered["status"], recovered["node"]) == ("recoverable", "start")
    assert recovered["finished_at"] is None
    assert controller._workers == {}

    # The replayed envelope is exactly the durable initial input: the registry record's
    # identity plus the pinned configuration path. Nothing from human recollection or
    # process memory.
    _wait_for(lambda: len(submissions) == 1)
    submitted_run_id, submitted_input = submissions[0]
    durable = registry.get_run(run_id)
    assert submitted_run_id == run_id
    assert submitted_input == {
        "project_id": _PROJECT_ID,
        "config_path": str(Path(durable["config_snapshot_path"]).expanduser().resolve()),
        "run_id": durable["id"],
        "thread_id": durable["thread_id"],
    }


def test_evidence_events_survive_across_independent_store_instances(tmp_path: Path) -> None:
    """Events/artifacts written by one EvidenceStore instance are read back intact."""
    evidence_root = tmp_path / "state" / "evidence"
    run_id = "run-evidence"
    task_id = "INV-STATE-001"

    writer = EvidenceStore(evidence_root)
    writer.append_event(run_id, "workflow_started", {"run_id": run_id, "node": "start"})
    writer.append_event(
        run_id,
        "node_completed",
        {"node": "first", "status": "first_done", "evidence": "checkpoint-preserved"},
    )
    writer.write_json(
        run_id,
        task_id,
        "continuation.json",
        {"source": "durable-checkpoint", "next": ["dead"]},
    )

    # Brand-new instance over the same state directory: no in-process cache.
    reader = EvidenceStore(evidence_root)
    assert reader.read_json(run_id, task_id, "continuation.json") == {
        "source": "durable-checkpoint",
        "next": ["dead"],
    }
    bundle = reader.read_task_bundle(run_id, task_id)
    assert bundle["run_id"] == run_id
    assert bundle["task_id"] == task_id
    assert bundle["artifacts"]["continuation.json"] == {
        "source": "durable-checkpoint",
        "next": ["dead"],
    }
    assert [found["run_id"] for found in reader.find_task_bundles(task_id)] == [run_id]

    events = [
        json.loads(line)
        for line in (reader.root / run_id / "events.jsonl").read_text(encoding="utf-8").splitlines()
    ]
    assert [event["event"] for event in events] == ["workflow_started", "node_completed"]
    assert events[0]["payload"] == {"run_id": run_id, "node": "start"}
    assert events[1]["payload"] == {
        "node": "first",
        "status": "first_done",
        "evidence": "checkpoint-preserved",
    }
    assert all(event["timestamp"] for event in events)
