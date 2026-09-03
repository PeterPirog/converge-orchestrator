from __future__ import annotations

import hashlib
import json
import types
from pathlib import Path
from unittest.mock import Mock, patch

from converge_orchestrator.models import ProjectConfig, TaskEnvelope
from converge_orchestrator.risk import RiskFinding, RiskReport
from converge_orchestrator.workflow import review


def _config(tmp_path: Path) -> ProjectConfig:
    requirements = tmp_path / "architecture.md"
    requirements.write_text("System must remain safe.\n", encoding="utf-8")
    return ProjectConfig(
        repo_path=tmp_path,
        requirements_path=requirements,
        require_spec_read_only=False,
        agents={},
    )


def _task() -> TaskEnvelope:
    return TaskEnvelope(
        id="ARCH-001-1",
        requirement_ids=["ARCH-001"],
        title="Change",
        objective="Change safely",
        allowed_paths=["src/**"],
    )


def _state(tmp_path: Path) -> dict:
    return {
        "config_path": str(tmp_path / "converge.yaml"),
        "run_id": "run-1",
        "task": _task().model_dump(mode="json"),
        "worktree": str(tmp_path),
        "approved_risk_flags": [],
    }


def _store():
    return types.SimpleNamespace(
        write_json=Mock(),
        write_text=Mock(),
        append_event=Mock(),
    )


def test_secret_material_suppresses_external_review_and_raw_diff_evidence(
    tmp_path: Path,
) -> None:
    cfg = _config(tmp_path)
    state = _state(tmp_path)
    store = _store()
    report = RiskReport(
        flags=["secret_material_detected"],
        findings=[
            RiskFinding(
                kind="secret_material",
                disposition="block",
                flag="secret_material_detected",
                path="src/settings.py",
                line=3,
                evidence="secret-like material detected; value redacted",
            )
        ],
    )

    with (
        patch("converge_orchestrator.workflow.load_config", return_value=cfg),
        patch("converge_orchestrator.workflow._requirements", return_value=[]),
        patch("converge_orchestrator.workflow.diff", return_value="+password='super-secret'"),
        patch("converge_orchestrator.workflow.classify_repository_risk", return_value=report),
        patch("converge_orchestrator.workflow._evidence", return_value=store),
        patch("converge_orchestrator.workflow.OpenCodeAdapter.invoke") as invoke,
    ):
        result = review(state)

    invoke.assert_not_called()
    store.write_text.assert_not_called()
    assert result["status"] == "risk_blocked"
    assert result["risk_flags"] == ["secret_material_detected"]
    assert result["review_result"]["verdict"] == "reject"


def test_planner_cannot_declare_deterministic_hard_block(tmp_path: Path) -> None:
    cfg = _config(tmp_path)
    state = _state(tmp_path)
    state["task"]["risk_flags"] = ["secret_material_detected"]
    store = _store()
    reviewer = types.SimpleNamespace(ok=True, output=json.dumps({"verdict": "pass"}))

    with (
        patch("converge_orchestrator.workflow.load_config", return_value=cfg),
        patch("converge_orchestrator.workflow._requirements", return_value=[]),
        patch("converge_orchestrator.workflow.diff", return_value="safe patch"),
        patch(
            "converge_orchestrator.workflow.classify_repository_risk",
            return_value=RiskReport(),
        ),
        patch("converge_orchestrator.workflow._evidence", return_value=store),
        patch(
            "converge_orchestrator.workflow.OpenCodeAdapter.invoke",
            return_value=reviewer,
        ) as invoke,
    ):
        result = review(state)

    invoke.assert_called_once()
    assert result["status"] == "reviewed"
    assert result["risk_flags"] == []
    assert result["review_result"]["verdict"] == "pass"


def test_risk_approval_is_invalidated_when_candidate_diff_changes(tmp_path: Path) -> None:
    cfg = _config(tmp_path)
    state = _state(tmp_path)
    state["approved_risk_flags"] = ["forbidden_public_api_change"]
    state["risk_fingerprint"] = hashlib.sha256(b"old patch").hexdigest()
    store = _store()
    report = RiskReport(
        flags=["forbidden_public_api_change"],
        findings=[
            RiskFinding(
                kind="public_api_break",
                disposition="interrupt",
                flag="forbidden_public_api_change",
                path="src/api.py",
                evidence="public Python signature changed: src/api.py:charge",
            )
        ],
    )
    reviewer = types.SimpleNamespace(ok=True, output=json.dumps({"verdict": "pass"}))

    with (
        patch("converge_orchestrator.workflow.load_config", return_value=cfg),
        patch("converge_orchestrator.workflow._requirements", return_value=[]),
        patch("converge_orchestrator.workflow.diff", return_value="new patch"),
        patch("converge_orchestrator.workflow.classify_repository_risk", return_value=report),
        patch("converge_orchestrator.workflow._evidence", return_value=store),
        patch("converge_orchestrator.workflow.OpenCodeAdapter.invoke", return_value=reviewer),
    ):
        result = review(state)

    assert result["approved_risk_flags"] == []
    assert result["risk_fingerprint"] == hashlib.sha256(b"new patch").hexdigest()


def test_risk_approval_survives_only_exact_same_candidate(tmp_path: Path) -> None:
    cfg = _config(tmp_path)
    patch_text = "same patch"
    fingerprint = hashlib.sha256(patch_text.encode()).hexdigest()
    state = _state(tmp_path)
    state["approved_risk_flags"] = ["forbidden_public_api_change"]
    state["risk_fingerprint"] = fingerprint
    store = _store()
    report = RiskReport(flags=["forbidden_public_api_change"])
    reviewer = types.SimpleNamespace(ok=True, output=json.dumps({"verdict": "pass"}))

    with (
        patch("converge_orchestrator.workflow.load_config", return_value=cfg),
        patch("converge_orchestrator.workflow._requirements", return_value=[]),
        patch("converge_orchestrator.workflow.diff", return_value=patch_text),
        patch("converge_orchestrator.workflow.classify_repository_risk", return_value=report),
        patch("converge_orchestrator.workflow._evidence", return_value=store),
        patch("converge_orchestrator.workflow.OpenCodeAdapter.invoke", return_value=reviewer),
    ):
        result = review(state)

    assert result["approved_risk_flags"] == ["forbidden_public_api_change"]
    assert result["risk_fingerprint"] == fingerprint
