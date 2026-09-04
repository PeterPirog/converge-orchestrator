from __future__ import annotations

import os
from pathlib import Path
from typing import TypedDict
from uuid import uuid4

import pytest
from langgraph.graph import END, START, StateGraph

from converge_orchestrator.persistence import PersistenceBackend, setup_postgres
from converge_orchestrator.registry_postgres import PostgresControlRegistry

DATABASE_URL = os.environ.get("CONVERGE_TEST_POSTGRES_URL")
pytestmark = pytest.mark.skipif(
    not DATABASE_URL,
    reason="CONVERGE_TEST_POSTGRES_URL is required for PostgreSQL integration tests",
)


class CounterState(TypedDict):
    value: int


def _counter_graph(checkpointer):
    builder = StateGraph(CounterState)
    builder.add_node("increment", lambda state: {"value": state["value"] + 1})
    builder.add_edge(START, "increment")
    builder.add_edge("increment", END)
    return builder.compile(checkpointer=checkpointer)


def test_postgres_registry_is_shared_and_lease_is_atomic(tmp_path: Path) -> None:
    assert DATABASE_URL is not None
    setup_postgres(DATABASE_URL)
    project_id = f"project-{uuid4().hex}"
    run_id = f"run-{uuid4().hex}"
    thread_id = f"thread-{uuid4().hex}"
    config_path = tmp_path / "converge.yaml"
    config_path.write_text("version: 1\n", encoding="utf-8")

    first = PostgresControlRegistry(DATABASE_URL)
    second = PostgresControlRegistry(DATABASE_URL)
    first.register_project(project_id, config_path)
    first.create_run(run_id, project_id, thread_id)

    assert second.get_project(project_id)["config_path"] == str(config_path.resolve())
    assert second.get_run(run_id)["thread_id"] == thread_id
    assert first.claim_run_lease(run_id, "worker-a", 60) is True
    assert second.claim_run_lease(run_id, "worker-b", 60) is False
    assert first.release_run_lease(run_id, "worker-a") is True
    assert second.claim_run_lease(run_id, "worker-b", 60) is True


def test_postgres_project_workspace_binding_is_shared_and_fail_closed(tmp_path: Path) -> None:
    assert DATABASE_URL is not None
    setup_postgres(DATABASE_URL)
    project_id = f"workspace-project-{uuid4().hex}"
    config_path = tmp_path / "converge.yaml"
    config_path.write_text("version: 1\n", encoding="utf-8")

    first = PostgresControlRegistry(DATABASE_URL)
    second = PostgresControlRegistry(DATABASE_URL)
    bound = first.register_project(project_id, config_path, workspace_id="workspace-a")

    assert bound["workspace_id"] == "workspace-a"
    assert second.get_project(project_id)["workspace_id"] == "workspace-a"
    assert second.register_project(
        project_id,
        config_path,
        workspace_id="workspace-a",
    )["workspace_id"] == "workspace-a"
    with pytest.raises(ValueError, match="already bound to workspace workspace-a"):
        second.register_project(project_id, config_path, workspace_id="workspace-b")


def test_postgres_langgraph_checkpoint_survives_connection_rotation(tmp_path: Path) -> None:
    assert DATABASE_URL is not None
    setup_postgres(DATABASE_URL)
    thread_id = f"checkpoint-{uuid4().hex}"
    backend = PersistenceBackend(tmp_path / "unused.sqlite", database_url=DATABASE_URL)
    graph_config = {"configurable": {"thread_id": thread_id}}

    checkpointer, first_db = backend.open_checkpointer(tmp_path)
    try:
        graph = _counter_graph(checkpointer)
        result = graph.invoke({"value": 41}, config=graph_config)
        assert result["value"] == 42
    finally:
        first_db.close()

    checkpointer, second_db = backend.open_checkpointer(tmp_path)
    try:
        graph = _counter_graph(checkpointer)
        snapshot = graph.get_state(graph_config)
        assert snapshot.values["value"] == 42
        assert not snapshot.next
    finally:
        second_db.close()
