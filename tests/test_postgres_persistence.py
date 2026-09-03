from __future__ import annotations

import os
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import TypedDict
from uuid import uuid4

import pytest
from langgraph.graph import END, START, StateGraph

from converge_orchestrator.runtime import RunController
from converge_orchestrator.storage import open_checkpointer, setup_checkpoint_storage

DSN = os.environ.get("CONVERGE_TEST_POSTGRES_DSN")
pytestmark = pytest.mark.skipif(not DSN, reason="PostgreSQL integration DSN is not configured")
pytest.importorskip("psycopg")
pytest.importorskip("langgraph.checkpoint.postgres")


class _TinyState(TypedDict, total=False):
    project_id: str
    config_path: str
    run_id: str
    thread_id: str
    status: str
    value: int


def _tiny_graph(checkpointer=None):
    graph = StateGraph(_TinyState)

    def finish(state: _TinyState) -> dict[str, object]:
        return {"status": "completed", "value": int(state.get("value", 0)) + 1}

    graph.add_node("finish", finish)
    graph.add_edge(START, "finish")
    graph.add_edge("finish", END)
    return graph.compile(checkpointer=checkpointer)


def test_postgres_checkpoint_setup_is_safe_under_concurrent_startup() -> None:
    assert DSN is not None
    with ThreadPoolExecutor(max_workers=4) as executor:
        results = list(executor.map(lambda _: setup_checkpoint_storage(DSN), range(4)))
    assert results == [None, None, None, None]


def test_postgres_registry_is_shared_and_lease_claim_is_atomic(tmp_path: Path) -> None:
    from converge_orchestrator.registry_postgres import PostgresControlRegistry

    assert DSN is not None
    project_id = f"project-{uuid4().hex}"
    run_id = f"run-{uuid4().hex}"
    first = PostgresControlRegistry(DSN)
    second = PostgresControlRegistry(DSN)
    first.register_project(project_id, tmp_path / "converge.yaml")
    first.create_run(run_id, project_id, f"thread-{uuid4().hex}")

    with ThreadPoolExecutor(max_workers=2) as executor:
        claims = list(
            executor.map(
                lambda owner: (
                    owner,
                    first.claim_run_lease(run_id, owner, 60)
                    if owner == "controller-a"
                    else second.claim_run_lease(run_id, owner, 60),
                ),
                ("controller-a", "controller-b"),
            )
        )

    winners = [owner for owner, claimed in claims if claimed]
    assert len(winners) == 1
    assert second.get_run(run_id)["lease_owner"] == winners[0]
    assert first.release_run_lease(run_id, winners[0])
    assert second.claim_run_lease(run_id, "controller-c", 60)


def test_postgres_checkpointer_persists_state_across_connections(tmp_path: Path) -> None:
    assert DSN is not None
    setup_checkpoint_storage(DSN)
    thread_id = f"thread-{uuid4().hex}"
    config = {"configurable": {"thread_id": thread_id}}

    saver, connection = open_checkpointer(tmp_path, DSN)
    try:
        graph = _tiny_graph(checkpointer=saver)
        graph.invoke({"value": 4}, config=config)
    finally:
        connection.close()

    restored_saver, restored_connection = open_checkpointer(tmp_path, DSN)
    try:
        restored = _tiny_graph(checkpointer=restored_saver).get_state(config)
    finally:
        restored_connection.close()

    assert restored.values["value"] == 5
    assert restored.values["status"] == "completed"


def test_run_controller_shares_registry_and_checkpoint_state(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    assert DSN is not None
    project_id = f"controller-{uuid4().hex}"
    repo = tmp_path / "repo"
    (repo / ".git").mkdir(parents=True)
    requirements = tmp_path / "architecture.md"
    requirements.write_text("System must remain deterministic.\n", encoding="utf-8")
    state_dir = tmp_path / "state"
    config_path = tmp_path / "converge.yaml"
    config_path.write_text(
        "\n".join(
            [
                "version: 1",
                "project:",
                f"  repo_path: {repo.as_posix()}",
                f"  requirements_path: {requirements.as_posix()}",
                f"  state_dir: {state_dir.as_posix()}",
                "  require_spec_read_only: false",
                "agents: {}",
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    monkeypatch.setattr("converge_orchestrator.runtime.build_graph", _tiny_graph)

    first = RunController(tmp_path / "unused-control.sqlite", postgres_dsn=DSN)
    first.register_project(project_id, config_path)
    record = first.start_run(project_id)
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        current = first.registry.get_run(record["id"])
        if current["finished_at"]:
            break
        time.sleep(0.05)
    else:
        pytest.fail("PostgreSQL-backed controller run did not complete")

    second = RunController(tmp_path / "another-unused-control.sqlite", postgres_dsn=DSN)
    restored = second.status(record["id"])
    assert restored["thread_id"] == record["thread_id"]
    assert restored["status"] == "completed"
    assert restored["values"]["status"] == "completed"
