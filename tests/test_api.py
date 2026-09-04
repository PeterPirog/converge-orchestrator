from __future__ import annotations

import json
import subprocess
from pathlib import Path

from fastapi.testclient import TestClient

from converge_orchestrator.api import _run_payload, create_app
from converge_orchestrator.evidence import EvidenceStore


def test_run_payload_exposes_checkpointed_human_decision_audit() -> None:
    raw = {
        "id": "run-1",
        "values": {
            "status": "human_retry_review",
            "human_decisions": [
                {
                    "sequence": 1,
                    "kind": "risk_policy",
                    "action": "approve",
                    "task_id": "ARCH-001-1",
                    "risk_flags": ["forbidden_public_api_change"],
                }
            ],
        },
    }

    payload = _run_payload(raw)

    assert payload["state"]["human_decisions"] == raw["values"]["human_decisions"]


def _project(tmp_path: Path) -> tuple[Path, Path]:
    repo = tmp_path / "repository"
    repo.mkdir()
    subprocess.run(["git", "init", "-b", "main", str(repo)], check=True, capture_output=True)
    subprocess.run(
        ["git", "-C", str(repo), "config", "user.email", "ci@example.invalid"],
        check=True,
    )
    subprocess.run(
        ["git", "-C", str(repo), "config", "user.name", "Converge CI"],
        check=True,
    )
    (repo / "README.md").write_text("fixture\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(repo), "add", "README.md"], check=True)
    subprocess.run(
        ["git", "-C", str(repo), "commit", "-m", "fixture"],
        check=True,
        capture_output=True,
    )

    requirements = tmp_path / "architecture.md"
    requirements.write_text(
        "# Architecture\n\nThe domain must not depend on infrastructure.\n",
        encoding="utf-8",
    )
    requirements.chmod(0o444)
    state_dir = tmp_path / ".converge"
    config = tmp_path / "project.yaml"
    config.write_text(
        "\n".join(
            [
                f'repo_path: "{repo}"',
                f'requirements_path: "{requirements}"',
                f'state_dir: "{state_dir}"',
                f'worktree_dir: "{state_dir / "worktrees"}"',
                "agents:",
                "  planner:",
                "    agent: converge-planner",
                "  builder:",
                "    agent: converge-builder",
                "  reviewer:",
                "    agent: converge-reviewer",
                "quality_gates: []",
                "",
            ]
        ),
        encoding="utf-8",
    )
    return config, state_dir


def test_api_bootstrap_compliance_evidence_and_pause(tmp_path: Path) -> None:
    config, state_dir = _project(tmp_path)
    app = create_app(tmp_path / "control.sqlite")
    client = TestClient(app)

    response = client.post(
        "/projects",
        json={"id": "payments", "config_path": str(config)},
    )
    assert response.status_code == 201

    response = client.get("/projects/payments/affinity")
    assert response.status_code == 200
    assert response.json() == {
        "project_id": "payments",
        "eligible": True,
        "basis": "project_config",
        "reason": "local",
        "unfinished_runs": 0,
    }

    response = client.post("/projects/payments/bootstrap")
    assert response.status_code == 200
    payload = response.json()
    assert payload["requirements"] == 1
    assert len(payload["requirements_hash"]) == 64

    response = client.get("/projects/payments/compliance")
    assert response.status_code == 200
    compliance = response.json()
    assert "entries" in compliance

    compliance_path = state_dir / "compliance.json"
    persisted = json.loads(compliance_path.read_text(encoding="utf-8"))
    requirement_id = next(iter(persisted["entries"]))
    persisted["entries"][requirement_id]["status"] = "pass"
    persisted["entries"][requirement_id]["evidence"] = ["fixture:verified"]
    compliance_path.write_text(json.dumps(persisted), encoding="utf-8")

    response = client.post("/projects/payments/bootstrap")
    assert response.status_code == 200
    response = client.get("/projects/payments/compliance")
    restored = response.json()["entries"][requirement_id]
    assert restored["status"] == "pass"
    assert restored["evidence"] == ["fixture:verified"]

    EvidenceStore(state_dir / "evidence").write_json(
        "run-evidence",
        "ARCH-001-1",
        "quality.json",
        {"ok": True},
    )
    response = client.get("/tasks/ARCH-001-1/evidence")
    assert response.status_code == 200
    assert response.json()["matches"][0]["artifacts"]["quality.json"] == {"ok": True}

    app.state.controller.registry.create_run("run-control", "payments", "thread-control")
    response = client.post("/runs/run-control/pause")
    assert response.status_code == 202
    assert response.json()["status"] == "pause_requested"


def test_api_bearer_token_protects_control_plane(tmp_path: Path) -> None:
    app = create_app(tmp_path / "control.sqlite", api_token="top-secret")
    client = TestClient(app)

    assert client.get("/health").status_code == 200

    response = client.get("/projects")
    assert response.status_code == 401
    assert response.headers["www-authenticate"] == "Bearer"

    response = client.get(
        "/projects",
        headers={"Authorization": "Bearer wrong-secret"},
    )
    assert response.status_code == 401

    response = client.get(
        "/projects",
        headers={"Authorization": "Bearer top-secret"},
    )
    assert response.status_code == 200
    assert response.json() == []


def test_openapi_exposes_required_mvp_control_routes(tmp_path: Path) -> None:
    app = create_app(tmp_path / "control.sqlite")
    routes = set(app.openapi()["paths"])
    expected = {
        "/projects",
        "/projects/{project_id}/affinity",
        "/projects/{project_id}/bootstrap",
        "/projects/{project_id}/run",
        "/runs/{run_id}",
        "/runs/{run_id}/pause",
        "/runs/{run_id}/resume",
        "/runs/{run_id}/interrupt",
        "/runs/{run_id}/decision",
        "/projects/{project_id}/compliance",
        "/tasks/{task_id}/evidence",
    }
    assert expected <= routes
