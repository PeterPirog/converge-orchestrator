from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from .acceptance import (
    AcceptanceCheck,
    ExternalAcceptanceReport,
    ExternalSupervisorEvidence,
    evaluate_external_acceptance,
)
from .models import ProjectConfig
from .spec import sha256_file

_REQUIRED_FINAL_LANES = {
    "requirements": "architecture_reviewer",
    "architecture": "architecture_reviewer",
    "compatibility": "correctness_reviewer",
    "security": "security_reviewer",
}


def _check(name: str, ok: bool, evidence: str) -> AcceptanceCheck:
    return AcceptanceCheck(name=name, ok=ok, evidence=evidence)


def _read_object(path: Path) -> tuple[dict[str, Any] | None, str | None]:
    if not path.is_file():
        return None, f"missing artifact: {path}"
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        return None, f"unreadable artifact {path}: {type(exc).__name__}: {exc}"
    if not isinstance(payload, dict):
        return None, f"artifact is not a JSON object: {path}"
    return payload, None


def _supervisor_journal_check(
    config: ProjectConfig,
    run_id: str,
    evidence: ExternalSupervisorEvidence | None,
) -> AcceptanceCheck:
    path = config.state_dir / "acceptance" / run_id / "supervisor-progress.json"
    payload, error = _read_object(path)
    if payload is None:
        return _check("supervisor_journal_provenance", False, error or "missing journal")
    if evidence is None:
        return _check(
            "supervisor_journal_provenance",
            False,
            "supervisor evidence is required to bind the journal",
        )

    candidate = payload.get("candidate_sha256")
    candidate_ok = (
        isinstance(candidate, str)
        and len(candidate) == 64
        and all(character in "0123456789abcdef" for character in candidate.lower())
    )
    restart = evidence.restart
    exception = evidence.exceptional_hitl
    ok = (
        payload.get("version") == 1
        and payload.get("run_id") == run_id
        and payload.get("restart_done") is True
        and payload.get("before_pid") == restart.before_pid
        and payload.get("after_pid") == restart.after_pid
        and payload.get("automatic_recovery_observed")
        is restart.automatic_recovery_observed
        and restart.automatic_recovery_observed
        and payload.get("hitl_done") is True
        and payload.get("hitl_action") == exception.action
        and exception.action == "approve"
        and payload.get("expected_risk_flag") == exception.expected_risk_flag
        and bool(str(exception.expected_risk_flag or "").strip())
        and payload.get("no_manual_code_edit") is exception.no_manual_code_edit
        and exception.no_manual_code_edit
        and isinstance(payload.get("expected_risk_flag"), str)
        and bool(str(payload.get("expected_risk_flag")).strip())
        and candidate_ok
    )
    return _check(
        "supervisor_journal_provenance",
        ok,
        (
            f"run={payload.get('run_id')}; restart={payload.get('restart_done')}; "
            f"pid={payload.get('before_pid')}->{payload.get('after_pid')}; "
            f"recovery={payload.get('automatic_recovery_observed')}; "
            f"hitl={payload.get('hitl_done')}:{payload.get('hitl_action')}; "
            f"candidate fingerprint={'present' if candidate_ok else 'invalid'}"
        ),
    )


def _final_audit_check(
    config: ProjectConfig,
    run_id: str,
    evidence: ExternalSupervisorEvidence | None,
) -> AcceptanceCheck:
    path = config.state_dir / "evidence" / run_id / "external-final-audit.json"
    payload, error = _read_object(path)
    if payload is None:
        return _check("final_audit_provenance", False, error or "missing final audit")
    if evidence is None:
        return _check(
            "final_audit_provenance",
            False,
            "supervisor evidence is required to bind the final audit",
        )

    try:
        requirements_hash = sha256_file(config.requirements_path)
    except OSError:
        requirements_hash = None
    target = str(config.github_repo or "")
    lanes = payload.get("lanes")
    lane_checks: list[bool] = []
    if isinstance(lanes, dict):
        for area, role in _REQUIRED_FINAL_LANES.items():
            lane = lanes.get(area)
            lane_checks.append(
                isinstance(lane, dict)
                and lane.get("area") == area
                and lane.get("role") == role
                and str(lane.get("verdict", "")).lower() == "pass"
                and str(evidence.final_independent_checks.get(area, "")).lower() == "pass"
            )
    else:
        lane_checks = [False]

    evidence_pass = (
        str(evidence.final_independent_checks.get("evidence", "")).lower() == "pass"
    )
    ok = (
        payload.get("version") == 1
        and payload.get("run_id") == run_id
        and target
        and str(payload.get("target_repository", "")).lower() == target.lower()
        and isinstance(requirements_hash, str)
        and payload.get("requirements_sha256") == requirements_hash
        and bool(lane_checks)
        and all(lane_checks)
        and payload.get("deterministic_evidence_ok") is True
        and evidence_pass
    )
    return _check(
        "final_audit_provenance",
        ok,
        (
            f"run={payload.get('run_id')}; target={payload.get('target_repository')}; "
            f"requirements hash match={payload.get('requirements_sha256') == requirements_hash}; "
            f"lanes pass={bool(lane_checks) and all(lane_checks)}; "
            f"deterministic evidence={payload.get('deterministic_evidence_ok')}"
        ),
    )


def evaluate_external_acceptance_with_provenance(
    config: ProjectConfig,
    status: dict[str, Any],
    *,
    supervisor_evidence: ExternalSupervisorEvidence | None,
) -> ExternalAcceptanceReport:
    """Evaluate the release gate and require live-supervisor provenance artifacts.

    A structurally valid operator-authored supervisor JSON is insufficient. The report must match
    the fixed progress journal and final-audit artifact emitted by the live acceptance supervisor
    for the same durable run. This is a procedural provenance boundary, not a replacement for
    filesystem integrity controls or a cryptographic signing system.
    """

    report = evaluate_external_acceptance(
        config,
        status,
        supervisor_evidence=supervisor_evidence,
    )
    checks = [
        *report.checks,
        _supervisor_journal_check(config, report.run_id, supervisor_evidence),
        _final_audit_check(config, report.run_id, supervisor_evidence),
    ]
    return report.model_copy(
        update={
            "checks": checks,
            "ready": all(check.ok for check in checks),
        }
    )
