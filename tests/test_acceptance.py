from __future__ import annotations

import json
from pathlib import Path

from converge_orchestrator.acceptance import (
    ExternalSupervisorEvidence,
    evaluate_external_acceptance,
)
from converge_orchestrator.models import ProjectConfig
from converge_orchestrator.spec import sha256_file

_PINNED_IMAGE = "ghcr.io/example/runtime@sha256:" + "a" * 64
_REVIEW_ROLES = ["correctness_reviewer", "architecture_reviewer", "security_reviewer"]


def _config(tmp_path: Path, *, github_repo: str = "example/target") -> ProjectConfig:
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / ".git").mkdir()
    requirements = tmp_path / "architecture.md"
    requirements.write_text(
        "ARCH-001 First mandatory requirement.\nARCH-002 Second mandatory requirement.\n",
        encoding="utf-8",
    )
    state_dir = tmp_path / "state"
    return ProjectConfig(
        project_name="acceptance-fixture",
        repo_path=repo,
        requirements_path=requirements,
        state_dir=state_dir,
        require_spec_read_only=False,
        github_repo=github_repo,
        sandbox={
            "mode": "container",
            "image": _PINNED_IMAGE,
            "agent_network": "converge-ai",
            "quality_network": "none",
        },
        agents={
            role: {"agent": f"converge-{role.replace('_', '-')}"}
            for role in _REVIEW_ROLES
        },
        review_roles=_REVIEW_ROLES,
    )


def _write_json(path: Path, payload: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload) + "\n", encoding="utf-8")


def _task_bundle(cfg: ProjectConfig, run_id: str, task_id: str, requirement_id: str) -> None:
    task_dir = cfg.state_dir / "evidence" / run_id / task_id
    task_dir.mkdir(parents=True, exist_ok=True)
    _write_json(
        task_dir / "task.json",
        {"id": task_id, "requirement_ids": [requirement_id]},
    )
    (task_dir / "diff.patch").write_text(
        "diff --git a/a.py b/a.py\n+change\n",
        encoding="utf-8",
    )
    _write_json(
        task_dir / "quality.json",
        [
            {"name": "tests", "required": True, "ok": True},
            {"name": "optional", "required": False, "ok": False},
        ],
    )
    _write_json(
        task_dir / "review.json",
        {
            "verdict": "pass",
            "findings": [],
            "reviewers": {role: "pass" for role in _REVIEW_ROLES},
        },
    )
    _write_json(task_dir / "risk.json", {"findings": [], "flags": []})
    _write_json(
        task_dir / "pr.json",
        {
            "url": f"https://github.com/example/target/pull/{task_id[-1]}",
            "head_sha": "a" * 40,
        },
    )
    _write_json(task_dir / "ci.json", {"status": "pass"})


def _status(cfg: ProjectConfig, run_id: str) -> dict:
    requirements_hash = sha256_file(cfg.requirements_path)
    return {
        "id": run_id,
        "project_id": "external-acceptance",
        "status": "converged",
        "finished_at": "2026-09-04T18:00:00+00:00",
        "values": {
            "requirements_hash": requirements_hash,
            "requirements": [
                {"id": "ARCH-001", "severity": "mandatory"},
                {"id": "ARCH-002", "severity": "mandatory"},
            ],
            "compliance": {
                "entries": {
                    "ARCH-001": {"status": "pass", "evidence": ["merged"]},
                    "ARCH-002": {"status": "pass", "evidence": ["merged"]},
                }
            },
        },
    }


def _supervisor(
    run_id: str,
    *,
    target_repository: str = "example/target",
) -> ExternalSupervisorEvidence:
    return ExternalSupervisorEvidence.model_validate(
        {
            "run_id": run_id,
            "target_repository": target_repository,
            "restart": {
                "before_pid": 100,
                "after_pid": 200,
                "automatic_recovery_observed": True,
            },
            "exceptional_hitl": {
                "kind": "risk_policy",
                "deliberately_injected": True,
                "action": "approve",
                "no_manual_code_edit": True,
            },
            "final_independent_checks": {
                "requirements": "pass",
                "architecture": "pass",
                "compatibility": "pass",
                "security": "pass",
                "evidence": "pass",
            },
        }
    )


def _complete_run(tmp_path: Path) -> tuple[ProjectConfig, str, dict]:
    cfg = _config(tmp_path)
    run_id = "run-acceptance"
    _task_bundle(cfg, run_id, "ARCH-001-1", "ARCH-001")
    _task_bundle(cfg, run_id, "ARCH-002-1", "ARCH-002")
    run_dir = cfg.state_dir / "evidence" / run_id
    events = [
        {"timestamp": "2026-09-04T17:00:00+00:00", "event": "bootstrap", "payload": {}},
        {
            "timestamp": "2026-09-04T17:20:00+00:00",
            "event": "merged",
            "payload": {"task_id": "ARCH-001-1", "sha": "1" * 40},
        },
        {
            "timestamp": "2026-09-04T17:50:00+00:00",
            "event": "merged",
            "payload": {"task_id": "ARCH-002-1", "sha": "2" * 40},
        },
    ]
    (run_dir / "events.jsonl").write_text(
        "".join(json.dumps(item) + "\n" for item in events),
        encoding="utf-8",
    )
    _write_json(
        run_dir / "run-budget.json",
        {
            "version": 1,
            "run_id": run_id,
            "started_at": "2026-09-04T17:00:00+00:00",
            "model_attempts_reserved": 20,
            "estimated_tokens_reserved": 200000,
            "updated_at": "2026-09-04T18:00:00+00:00",
        },
    )
    return cfg, run_id, _status(cfg, run_id)


def test_external_acceptance_passes_only_with_complete_machine_and_supervisor_evidence(
    tmp_path: Path,
) -> None:
    cfg, run_id, status = _complete_run(tmp_path)

    report = evaluate_external_acceptance(
        cfg,
        status,
        supervisor_evidence=_supervisor(run_id),
    )

    assert report.ready is True
    assert report.merged_task_ids == ["ARCH-001-1", "ARCH-002-1"]
    assert all(check.ok for check in report.checks)


def test_external_acceptance_requires_process_restart_hitl_and_final_audit_proof(
    tmp_path: Path,
) -> None:
    cfg, _run_id, status = _complete_run(tmp_path)

    report = evaluate_external_acceptance(cfg, status)

    assert report.ready is False
    failed = {check.name for check in report.checks if not check.ok}
    assert {"controller_restart", "exceptional_hitl", "final_independent_audit"} <= failed


def test_external_acceptance_requires_multiple_merged_cycles(tmp_path: Path) -> None:
    cfg, run_id, status = _complete_run(tmp_path)
    events_path = cfg.state_dir / "evidence" / run_id / "events.jsonl"
    events = [json.loads(line) for line in events_path.read_text(encoding="utf-8").splitlines()]
    events = [
        item
        for item in events
        if not (item["event"] == "merged" and item["payload"].get("task_id") == "ARCH-002-1")
    ]
    events_path.write_text(
        "".join(json.dumps(item) + "\n" for item in events),
        encoding="utf-8",
    )

    report = evaluate_external_acceptance(
        cfg,
        status,
        supervisor_evidence=_supervisor(run_id),
    )

    assert report.ready is False
    check = next(item for item in report.checks if item.name == "multiple_autonomous_cycles")
    assert check.ok is False


def test_external_acceptance_detects_requirement_drift(tmp_path: Path) -> None:
    cfg, run_id, status = _complete_run(tmp_path)
    cfg.requirements_path.write_text("ARCH-001 changed after the run.\n", encoding="utf-8")

    report = evaluate_external_acceptance(
        cfg,
        status,
        supervisor_evidence=_supervisor(run_id),
    )

    assert report.ready is False
    check = next(item for item in report.checks if item.name == "immutable_requirements")
    assert check.ok is False


def test_external_acceptance_rejects_orchestrator_itself_as_target(tmp_path: Path) -> None:
    cfg, run_id, status = _complete_run(tmp_path)
    cfg.github_repo = "PeterPirog/converge-orchestrator"

    report = evaluate_external_acceptance(
        cfg,
        status,
        supervisor_evidence=_supervisor(
            run_id,
            target_repository="PeterPirog/converge-orchestrator",
        ),
    )

    assert report.ready is False
    check = next(item for item in report.checks if item.name == "external_target_repository")
    assert check.ok is False
