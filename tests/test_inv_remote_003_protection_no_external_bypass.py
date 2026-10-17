"""Lock INV-REMOTE-003: protected branch never authorizes an external-policy bypass.

INV-REMOTE-003 (CONVERGE_ORCHESTRATOR_2_0_SOURCE_OF_TRUTH.md:L163 [4. Global invariants]):
Presence of remote branch protection SHALL NOT authorize Converge to bypass any applicable
external policy. INV-REMOTE-001/002 fixed that protection is an additional layer whose absence
never weakens local gates; this locks the authorization direction: a *present, authoritative*
protected policy must not be used as a license to skip an applicable external policy.

These deterministic offline tests build a genuinely ``ready=True`` baseline (a protected,
authoritative, green CI policy plus full supervisor evidence) under ``tmp_path`` and then break
each external policy in turn, asserting the per-task ``:ci`` check stays green while the broken
policy is the specific failing check:

* ``final_compliance`` — breaking a mandatory compliance verdict;
* ``immutable_requirements`` — drifting the Source of Truth after the run;
* supervisor provenance — omitting the external supervisor evidence;
* zero required checks — ``protected=True`` alone still fails closed in the CI gate.

No network, model, subprocess, or git calls.
"""

from __future__ import annotations

import json
from pathlib import Path

from converge_orchestrator.acceptance import (
    AcceptanceCheck,
    ExternalSupervisorEvidence,
    _authoritative_ci_policy,
    evaluate_external_acceptance,
)
from converge_orchestrator.models import ProjectConfig
from converge_orchestrator.spec import sha256_file

_PINNED_IMAGE = "ghcr.io/example/runtime@sha256:" + "a" * 64
_REVIEW_ROLES = ["correctness_reviewer", "architecture_reviewer", "security_reviewer"]


def _write_json(path: Path, payload: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload) + "\n", encoding="utf-8")


def _config(tmp_path: Path) -> ProjectConfig:
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
        project_name="inv-remote-003-fixture",
        repo_path=repo,
        requirements_path=requirements,
        state_dir=state_dir,
        require_spec_read_only=False,
        github_repo="example/target",
        sandbox={
            "mode": "container",
            "image": _PINNED_IMAGE,
            "agent_network": "converge-ai",
            "quality_network": "none",
        },
        agents={role: {"agent": f"converge-{role.replace('_', '-')}"} for role in _REVIEW_ROLES},
        review_roles=_REVIEW_ROLES,
    )


def _remote_policy(required_checks: list[dict]) -> dict:
    return {
        "kind": "remote_policy",
        "base_branch": "main",
        "protected": True,
        "authoritative": True,
        "source": "branch_protection",
        "required_checks": required_checks,
    }


def _protected_green_ci_payload() -> dict:
    """A present, authoritative branch-protection policy with one required check."""
    return {
        "status": "pass",
        "checks": [_remote_policy([{"context": "test", "app_id": 15368}])],
    }


def _task_bundle(cfg: ProjectConfig, run_id: str, task_id: str, requirement_id: str) -> None:
    task_dir = cfg.state_dir / "evidence" / run_id / task_id
    task_dir.mkdir(parents=True, exist_ok=True)
    _write_json(task_dir / "task.json", {"id": task_id, "requirement_ids": [requirement_id]})
    (task_dir / "diff.patch").write_text("diff --git a/a.py b/a.py\n+change\n", encoding="utf-8")
    _write_json(
        task_dir / "quality.json",
        [
            {"name": "tests", "required": True, "ok": True},
            {"name": "optional", "required": False, "ok": False},
        ],
    )
    _write_json(
        task_dir / "review.json",
        {"verdict": "pass", "findings": [], "reviewers": {role: "pass" for role in _REVIEW_ROLES}},
    )
    _write_json(task_dir / "risk.json", {"findings": [], "flags": []})
    _write_json(
        task_dir / "pr.json",
        {"url": f"https://github.com/example/target/pull/{task_id[-1]}", "head_sha": "a" * 40},
    )
    _write_json(task_dir / "ci.json", _protected_green_ci_payload())


def _status(cfg: ProjectConfig, run_id: str) -> dict:
    return {
        "id": run_id,
        "project_id": "inv-remote-003",
        "status": "converged",
        "finished_at": "2026-09-04T18:00:00+00:00",
        "values": {
            "requirements_hash": sha256_file(cfg.requirements_path),
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
            "human_decisions": [
                {
                    "sequence": 1,
                    "kind": "risk_policy",
                    "action": "approve",
                    "task_id": "ARCH-002-1",
                    "risk_flags": ["forbidden_public_api_change"],
                }
            ],
        },
    }


def _supervisor(run_id: str) -> ExternalSupervisorEvidence:
    return ExternalSupervisorEvidence.model_validate(
        {
            "run_id": run_id,
            "target_repository": "example/target",
            "restart": {"before_pid": 100, "after_pid": 200, "automatic_recovery_observed": True},
            "exceptional_hitl": {
                "kind": "risk_policy",
                "expected_risk_flag": "forbidden_public_api_change",
                "deliberately_injected": True,
                "action": "approve",
                "no_manual_code_edit": True,
            },
            "final_independent_checks": {
                name: "pass" for name in (
                    "requirements", "architecture", "compatibility", "security", "evidence"
                )
            },
        }
    )


def _complete_run(tmp_path: Path) -> tuple[ProjectConfig, str, dict]:
    cfg = _config(tmp_path)
    run_id = "run-inv-remote-003"
    _task_bundle(cfg, run_id, "ARCH-001-1", "ARCH-001")
    _task_bundle(cfg, run_id, "ARCH-002-1", "ARCH-002")
    run_dir = cfg.state_dir / "evidence" / run_id
    events = [
        {"event": "bootstrap", "payload": {}},
        {"event": "merged", "payload": {"task_id": "ARCH-001-1", "sha": "1" * 40}},
        {"event": "merged", "payload": {"task_id": "ARCH-002-1", "sha": "2" * 40}},
    ]
    (run_dir / "events.jsonl").write_text(
        "".join(json.dumps(item) + "\n" for item in events), encoding="utf-8"
    )
    _write_json(
        run_dir / "run-budget.json",
        {
            "run_id": run_id,
            "model_attempts_reserved": 20,
            "estimated_tokens_reserved": 200000,
        },
    )
    return cfg, run_id, _status(cfg, run_id)


def _ci_checks(report) -> list[AcceptanceCheck]:
    return [check for check in report.checks if check.name.endswith(":ci")]


def test_protected_green_ci_plus_supervisor_evidence_is_ready(tmp_path: Path) -> None:
    """Positive control: a protected, authoritative green CI policy is honored, not waived."""
    cfg, run_id, status = _complete_run(tmp_path)

    report = evaluate_external_acceptance(cfg, status, supervisor_evidence=_supervisor(run_id))

    assert report.ready is True
    assert all(check.ok for check in report.checks)
    ci_checks = _ci_checks(report)
    assert ci_checks, "expected a per-task CI check in the report"
    for check in ci_checks:
        assert check.ok is True
        assert "policy=branch_protection" in check.evidence
        assert "required_checks=1" in check.evidence


def test_protected_green_ci_does_not_bypass_mandatory_compliance(tmp_path: Path) -> None:
    """A present protected branch must not waive a broken mandatory compliance verdict."""
    cfg, run_id, status = _complete_run(tmp_path)
    status["values"]["compliance"]["entries"]["ARCH-002"]["status"] = "fail"

    report = evaluate_external_acceptance(cfg, status, supervisor_evidence=_supervisor(run_id))

    assert report.ready is False
    assert all(check.ok for check in _ci_checks(report)), "protected green CI must remain green"
    assert "final_compliance" in {check.name for check in report.checks if not check.ok}


def test_protected_green_ci_does_not_bypass_immutable_requirements(tmp_path: Path) -> None:
    """A present protected branch must not waive post-run drift of the immutable Source of Truth."""
    cfg, run_id, status = _complete_run(tmp_path)
    cfg.requirements_path.write_text("ARCH-001 changed after the run.\n", encoding="utf-8")

    report = evaluate_external_acceptance(cfg, status, supervisor_evidence=_supervisor(run_id))

    assert report.ready is False
    assert all(check.ok for check in _ci_checks(report)), "protected green CI must remain green"
    assert "immutable_requirements" in {check.name for check in report.checks if not check.ok}


def test_protected_green_ci_does_not_bypass_supervisor_provenance(tmp_path: Path) -> None:
    """A present protected branch must not waive the external supervisor provenance."""
    cfg, run_id, status = _complete_run(tmp_path)

    report = evaluate_external_acceptance(cfg, status)

    assert report.ready is False
    assert all(check.ok for check in _ci_checks(report)), "protected green CI must remain green"
    failing = {check.name for check in report.checks if not check.ok}
    assert {"controller_restart", "exceptional_hitl", "final_independent_audit"} <= failing


def test_protected_authoritative_policy_with_zero_required_checks_is_not_ok() -> None:
    """protected=True with an empty required_checks list fails closed (no protection-only grant)."""
    ci = {"status": "pass", "checks": [_remote_policy([])]}

    ok, count, source = _authoritative_ci_policy(ci)

    assert ok is False
    assert count == 0
    assert source == "branch_protection"
