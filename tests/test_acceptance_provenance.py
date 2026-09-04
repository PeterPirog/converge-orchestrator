from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch

from converge_orchestrator.acceptance import (
    AcceptanceCheck,
    ExternalAcceptanceReport,
    ExternalSupervisorEvidence,
)
from converge_orchestrator.acceptance_provenance import (
    evaluate_external_acceptance_with_provenance,
)
from converge_orchestrator.models import ProjectConfig


def _config(tmp_path: Path) -> ProjectConfig:
    repo = tmp_path / "repo"
    repo.mkdir()
    requirements = tmp_path / "architecture.md"
    requirements.write_text("ARCH-001 Preserve behavior.\n", encoding="utf-8")
    return ProjectConfig(
        project_name="acceptance",
        repo_path=repo,
        requirements_path=requirements,
        state_dir=tmp_path / "state",
        github_repo="example/target",
        agents={},
        require_spec_read_only=False,
    )


def _supervisor() -> ExternalSupervisorEvidence:
    return ExternalSupervisorEvidence.model_validate(
        {
            "run_id": "run-1",
            "target_repository": "example/target",
            "restart": {
                "before_pid": 101,
                "after_pid": 202,
                "automatic_recovery_observed": True,
            },
            "exceptional_hitl": {
                "kind": "risk_policy",
                "expected_risk_flag": "forbidden_public_api_change",
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


def _base_report() -> ExternalAcceptanceReport:
    return ExternalAcceptanceReport(
        run_id="run-1",
        project_id="acceptance",
        target_repository="example/target",
        ready=True,
        merged_task_ids=["ARCH-001-1", "ARCH-001-2"],
        checks=[AcceptanceCheck(name="base", ok=True, evidence="pass")],
    )


def _write_provenance(cfg: ProjectConfig, *, audit_role: str = "correctness_reviewer") -> None:
    progress_path = cfg.state_dir / "acceptance" / "run-1" / "supervisor-progress.json"
    progress_path.parent.mkdir(parents=True, exist_ok=True)
    progress_path.write_text(
        json.dumps(
            {
                "version": 1,
                "run_id": "run-1",
                "restart_done": True,
                "before_pid": 101,
                "after_pid": 202,
                "automatic_recovery_observed": True,
                "hitl_done": True,
                "hitl_action": "approve",
                "expected_risk_flag": "forbidden_public_api_change",
                "candidate_sha256": "a" * 64,
                "no_manual_code_edit": True,
            }
        ),
        encoding="utf-8",
    )

    from converge_orchestrator.spec import sha256_file

    audit_path = cfg.state_dir / "evidence" / "run-1" / "external-final-audit.json"
    audit_path.parent.mkdir(parents=True, exist_ok=True)
    audit_path.write_text(
        json.dumps(
            {
                "version": 1,
                "run_id": "run-1",
                "target_repository": "example/target",
                "requirements_sha256": sha256_file(cfg.requirements_path),
                "lanes": {
                    "requirements": {
                        "area": "requirements",
                        "role": "architecture_reviewer",
                        "verdict": "pass",
                        "findings": [],
                    },
                    "architecture": {
                        "area": "architecture",
                        "role": "architecture_reviewer",
                        "verdict": "pass",
                        "findings": [],
                    },
                    "compatibility": {
                        "area": "compatibility",
                        "role": audit_role,
                        "verdict": "pass",
                        "findings": [],
                    },
                    "security": {
                        "area": "security",
                        "role": "security_reviewer",
                        "verdict": "pass",
                        "findings": [],
                    },
                },
                "deterministic_evidence_ok": True,
            }
        ),
        encoding="utf-8",
    )


def test_hand_authored_supervisor_json_without_live_artifacts_fails_closed(tmp_path: Path) -> None:
    cfg = _config(tmp_path)
    with patch(
        "converge_orchestrator.acceptance_provenance.evaluate_external_acceptance",
        return_value=_base_report(),
    ):
        result = evaluate_external_acceptance_with_provenance(
            cfg,
            {},
            supervisor_evidence=_supervisor(),
        )

    assert result.ready is False
    failures = {check.name for check in result.checks if not check.ok}
    assert failures == {"supervisor_journal_provenance", "final_audit_provenance"}


def test_matching_live_supervisor_artifacts_satisfy_provenance(tmp_path: Path) -> None:
    cfg = _config(tmp_path)
    _write_provenance(cfg)
    with patch(
        "converge_orchestrator.acceptance_provenance.evaluate_external_acceptance",
        return_value=_base_report(),
    ):
        result = evaluate_external_acceptance_with_provenance(
            cfg,
            {},
            supervisor_evidence=_supervisor(),
        )

    assert result.ready is True
    provenance = {check.name: check.ok for check in result.checks if "provenance" in check.name}
    assert provenance == {
        "supervisor_journal_provenance": True,
        "final_audit_provenance": True,
    }


def test_mismatched_final_audit_lane_fails_closed(tmp_path: Path) -> None:
    cfg = _config(tmp_path)
    _write_provenance(cfg, audit_role="architecture_reviewer")
    with patch(
        "converge_orchestrator.acceptance_provenance.evaluate_external_acceptance",
        return_value=_base_report(),
    ):
        result = evaluate_external_acceptance_with_provenance(
            cfg,
            {},
            supervisor_evidence=_supervisor(),
        )

    assert result.ready is False
    check = next(item for item in result.checks if item.name == "final_audit_provenance")
    assert check.ok is False
