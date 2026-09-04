from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

from fastapi.testclient import TestClient

from converge_orchestrator.api import create_app
from converge_orchestrator.observability import (
    collect_registry_snapshot,
    render_prometheus,
)


class _Registry:
    def __init__(self) -> None:
        self.projects = [
            {"id": "payments", "workspace_id": "w1", "state_store_id": "s1"},
            {"id": "catalog", "workspace_id": "w2", "state_store_id": None},
        ]
        self.runs = {
            "payments": [],
            "catalog": [],
        }

    def list_projects(self) -> list[dict]:
        return list(self.projects)

    def runs_for_project(self, project_id: str) -> list[dict]:
        return list(self.runs[project_id])


def test_registry_snapshot_is_durable_low_cardinality_state() -> None:
    now = datetime(2026, 9, 4, 5, 0, tzinfo=UTC)
    registry = _Registry()
    registry.runs["payments"] = [
        {
            "id": "run-active-secret-id",
            "status": "running",
            "started_at": (now - timedelta(minutes=5)).isoformat(),
            "finished_at": None,
            "error": None,
            "lease_owner": "controller-private-id",
            "lease_expires_at": (now + timedelta(seconds=30)).isoformat(),
            "config_snapshot_path": "/private/config.json",
            "config_snapshot_sha256": "a" * 64,
        },
        {
            "id": "run-failed-secret-id",
            "status": "failed",
            "started_at": (now - timedelta(hours=1)).isoformat(),
            "finished_at": (now - timedelta(minutes=30)).isoformat(),
            "error": "private failure detail",
            "lease_owner": None,
            "lease_expires_at": None,
            "config_snapshot_path": None,
            "config_snapshot_sha256": None,
        },
    ]
    registry.runs["catalog"] = [
        {
            "id": "run-malformed-secret-id",
            "status": "waiting_ci",
            "started_at": (now - timedelta(minutes=20)).isoformat(),
            "finished_at": None,
            "error": None,
            "lease_owner": "controller-other-private-id",
            "lease_expires_at": None,
            "config_snapshot_path": "/private/incomplete.json",
            "config_snapshot_sha256": None,
        }
    ]

    snapshot = collect_registry_snapshot(registry, "postgres", now=now)

    assert snapshot["projects"] == 2
    assert snapshot["project_affinity"] == {"complete": 1, "incomplete": 1}
    assert snapshot["runs"] == 3
    assert snapshot["runs_unfinished"] == 2
    assert snapshot["runs_by_status"] == {"failed": 1, "running": 1, "waiting_ci": 1}
    assert snapshot["runs_with_error"] == 1
    assert snapshot["leases"] == {"active": 1, "malformed": 1}
    assert snapshot["config_snapshots"] == {
        "incomplete": 1,
        "legacy_unpinned": 1,
        "pinned": 1,
    }
    assert snapshot["oldest_unfinished_age_seconds"] == 1200.0
    assert snapshot["timestamp_parse_issues"] == 0

    metrics = render_prometheus(snapshot)
    assert 'converge_persistence_backend_info{backend="postgres"} 1' in metrics
    assert 'converge_runs_by_status{status="waiting_ci"} 1' in metrics
    assert 'converge_run_leases{state="active"} 1' in metrics
    assert 'converge_run_config_snapshots{state="incomplete"} 1' in metrics
    assert 'converge_project_affinity{state="incomplete"} 1' in metrics
    assert "run-active-secret-id" not in metrics
    assert "controller-private-id" not in metrics
    assert "/private/config.json" not in metrics
    assert "private failure detail" not in metrics


def test_malformed_registry_timestamps_are_observable_not_fatal() -> None:
    now = datetime(2026, 9, 4, 5, 0, tzinfo=UTC)
    registry = _Registry()
    registry.projects = [{"id": "payments", "workspace_id": "w", "state_store_id": "s"}]
    registry.runs["payments"] = [
        {
            "id": "run-1",
            "status": "recoverable",
            "started_at": "not-a-timestamp",
            "finished_at": None,
            "error": None,
            "lease_owner": "owner",
            "lease_expires_at": "also-not-a-timestamp",
            "config_snapshot_path": None,
            "config_snapshot_sha256": None,
        }
    ]

    snapshot = collect_registry_snapshot(registry, "sqlite", now=now)

    assert snapshot["runs_unfinished"] == 1
    assert snapshot["leases"] == {"malformed": 1}
    assert snapshot["timestamp_parse_issues"] == 2
    assert snapshot["oldest_unfinished_age_seconds"] == 0.0


def test_diagnostics_and_metrics_share_control_plane_authentication(tmp_path: Path) -> None:
    app = create_app(tmp_path / "control.sqlite", api_token="observability-secret")
    registry = app.state.controller.registry
    registry.register_project(
        "payments",
        tmp_path / "project.yaml",
        workspace_id="11111111-1111-1111-1111-111111111111",
        state_store_id="22222222-2222-2222-2222-222222222222",
    )
    registry.create_run("run-1", "payments", "thread-1")
    registry.update_run("run-1", status="waiting_ci", node="ci_wait")
    client = TestClient(app)

    assert client.get("/diagnostics").status_code == 401
    assert client.get("/metrics").status_code == 401

    headers = {"Authorization": "Bearer observability-secret"}
    diagnostics = client.get("/diagnostics", headers=headers)
    assert diagnostics.status_code == 200
    payload = diagnostics.json()
    assert payload["persistence_backend"] == "sqlite"
    assert payload["projects"] == 1
    assert payload["runs_by_status"] == {"waiting_ci": 1}

    metrics = client.get("/metrics", headers=headers)
    assert metrics.status_code == 200
    assert metrics.headers["content-type"].startswith("text/plain; version=0.0.4")
    assert 'converge_runs_by_status{status="waiting_ci"} 1' in metrics.text
