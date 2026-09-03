from __future__ import annotations

import types
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import Mock, patch

from converge_orchestrator.ci import ci_poll, ci_wait, route_after_ci
from converge_orchestrator.ci_flakes import FlakyCIPolicy
from converge_orchestrator.graph_service import build_graph
from converge_orchestrator.models import CIResult, ProjectConfig, TaskEnvelope


def _config(
    tmp_path: Path,
    *,
    timeout: int = 1800,
    poll: int = 15,
) -> ProjectConfig:
    return ProjectConfig(
        repo_path=tmp_path,
        requirements_path=tmp_path / "architecture.md",
        github_repo="owner/repo",
        agents={},
        ci_timeout_seconds=timeout,
        ci_poll_seconds=poll,
    )


def _task() -> TaskEnvelope:
    return TaskEnvelope(
        id="ARCH-001-1",
        requirement_ids=["ARCH-001"],
        title="Change",
        objective="Change safely",
    )


def _state() -> dict:
    return {
        "config_path": "converge.yaml",
        "run_id": "run-1",
        "task": _task().model_dump(mode="json"),
        "pr": {
            "number": 1,
            "url": "https://example.test/pr/1",
            "head_sha": "abc",
        },
        "repair_attempts": 0,
        "replan_attempts": 0,
    }


def _store():
    return types.SimpleNamespace(write_json=Mock(), append_event=Mock())


def test_ci_poll_observes_github_exactly_once(tmp_path: Path) -> None:
    cfg = _config(tmp_path)
    store = _store()
    adapter = Mock()
    adapter.ci_status.return_value = CIResult(status="pending", head_sha="abc")

    with (
        patch("converge_orchestrator.ci.load_config", return_value=cfg),
        patch(
            "converge_orchestrator.ci.load_flaky_ci_policy",
            return_value=FlakyCIPolicy(),
        ),
        patch("converge_orchestrator.ci.wf._evidence", return_value=store),
        patch("converge_orchestrator.ci.GitHubAdapter", return_value=adapter),
    ):
        result = ci_poll(_state())

    adapter.ci_status.assert_called_once_with("abc")
    assert result["status"] == "ci_pending"
    assert result["ci"]["status"] == "pending"
    assert result["ci_head_sha"] == "abc"
    assert result["ci_started_at"]


def test_ci_timeout_is_measured_across_checkpointed_polls(tmp_path: Path) -> None:
    cfg = _config(tmp_path, timeout=30)
    store = _store()
    adapter = Mock()
    adapter.ci_status.return_value = CIResult(status="pending", head_sha="abc")
    now = datetime(2026, 9, 3, 10, 0, tzinfo=UTC)
    state = _state()
    state["ci_head_sha"] = "abc"
    state["ci_started_at"] = (now - timedelta(seconds=31)).isoformat()

    with (
        patch("converge_orchestrator.ci.load_config", return_value=cfg),
        patch(
            "converge_orchestrator.ci.load_flaky_ci_policy",
            return_value=FlakyCIPolicy(),
        ),
        patch("converge_orchestrator.ci.wf._evidence", return_value=store),
        patch("converge_orchestrator.ci.GitHubAdapter", return_value=adapter),
        patch("converge_orchestrator.ci._utcnow", return_value=now),
    ):
        result = ci_poll(state)

    assert result["ci"]["status"] == "timeout"
    assert result["status"] == "ci_timeout"
    assert result["ci_started_at"] == state["ci_started_at"]


def test_new_candidate_head_resets_ci_timeout_window(tmp_path: Path) -> None:
    cfg = _config(tmp_path, timeout=30)
    store = _store()
    adapter = Mock()
    adapter.ci_status.return_value = CIResult(status="pending", head_sha="abc")
    now = datetime(2026, 9, 3, 10, 0, tzinfo=UTC)
    state = _state()
    state["ci_head_sha"] = "old"
    state["ci_started_at"] = (now - timedelta(hours=2)).isoformat()

    with (
        patch("converge_orchestrator.ci.load_config", return_value=cfg),
        patch(
            "converge_orchestrator.ci.load_flaky_ci_policy",
            return_value=FlakyCIPolicy(),
        ),
        patch("converge_orchestrator.ci.wf._evidence", return_value=store),
        patch("converge_orchestrator.ci.GitHubAdapter", return_value=adapter),
        patch("converge_orchestrator.ci._utcnow", return_value=now),
    ):
        result = ci_poll(state)

    assert result["ci"]["status"] == "pending"
    assert result["ci_started_at"] == now.isoformat()


def test_ci_wait_interrupt_contains_durable_wake_time(tmp_path: Path) -> None:
    cfg = _config(tmp_path, poll=20)
    now = datetime(2026, 9, 3, 10, 0, tzinfo=UTC)
    state = _state()
    state["ci"] = CIResult(
        status="pending",
        head_sha="abc",
    ).model_dump(mode="json")

    with (
        patch("converge_orchestrator.ci.load_config", return_value=cfg),
        patch("converge_orchestrator.ci._utcnow", return_value=now),
        patch(
            "converge_orchestrator.ci.interrupt",
            return_value="resume",
        ) as interrupt_call,
    ):
        result = ci_wait(state)

    payload = interrupt_call.call_args.args[0]
    assert payload["kind"] == "ci_wait"
    assert payload["wake_at"] == (now + timedelta(seconds=20)).isoformat()
    assert result["status"] == "ci_wait_elapsed"


def test_pending_ci_routes_to_machine_wait(tmp_path: Path) -> None:
    cfg = _config(tmp_path)
    state = _state()
    state["ci"] = CIResult(
        status="pending",
        head_sha="abc",
    ).model_dump(mode="json")
    with patch("converge_orchestrator.ci.load_config", return_value=cfg):
        assert route_after_ci(state) == "wait"


def test_service_graph_uses_checkpointable_ci_nodes() -> None:
    graph = build_graph()
    assert "ci_poll" in graph.nodes
    assert "ci_wait" in graph.nodes
    assert "ci" not in graph.nodes
