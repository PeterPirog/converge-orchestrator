from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field, ValidationError

from .models import ProjectConfig
from .sandbox import _is_digest_pinned_image
from .spec import sha256_file

_ORCHESTRATOR_REPOSITORY = "peterpirog/converge-orchestrator"
_REQUIRED_TASK_ARTIFACTS = (
    "task.json",
    "diff.patch",
    "quality.json",
    "review.json",
    "risk.json",
    "pr.json",
    "ci.json",
)


class AcceptanceCheck(BaseModel):
    name: str
    ok: bool
    evidence: str


class SupervisorRestartEvidence(BaseModel):
    before_pid: int = Field(gt=0)
    after_pid: int = Field(gt=0)
    automatic_recovery_observed: bool


class SupervisorExceptionEvidence(BaseModel):
    kind: str
    deliberately_injected: bool
    action: str
    no_manual_code_edit: bool


class ExternalSupervisorEvidence(BaseModel):
    """Evidence emitted by the external acceptance supervisor, not by an agent."""

    version: int = 1
    run_id: str
    target_repository: str
    restart: SupervisorRestartEvidence
    exceptional_hitl: SupervisorExceptionEvidence
    final_independent_checks: dict[str, str]


class ExternalAcceptanceReport(BaseModel):
    version: int = 1
    run_id: str
    project_id: str
    target_repository: str | None
    ready: bool
    merged_task_ids: list[str]
    checks: list[AcceptanceCheck]


def _check(name: str, ok: bool, evidence: str) -> AcceptanceCheck:
    return AcceptanceCheck(name=name, ok=ok, evidence=evidence)


def _read_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def _read_events(path: Path) -> tuple[list[dict[str, Any]], str | None]:
    if not path.is_file():
        return [], f"missing event stream: {path}"
    events: list[dict[str, Any]] = []
    try:
        for line_number, raw in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            if not raw.strip():
                continue
            payload = json.loads(raw)
            if not isinstance(payload, dict) or not isinstance(payload.get("event"), str):
                return [], f"invalid event record at line {line_number}"
            if not isinstance(payload.get("payload"), dict):
                return [], f"invalid event payload at line {line_number}"
            events.append(payload)
    except (OSError, json.JSONDecodeError) as exc:
        return [], f"event stream is unreadable: {type(exc).__name__}: {exc}"
    return events, None


def _merged_task_ids(events: list[dict[str, Any]]) -> list[str]:
    output: list[str] = []
    seen: set[str] = set()
    for event in events:
        if event.get("event") != "merged":
            continue
        task_id = str(event["payload"].get("task_id") or "").strip()
        if task_id and task_id not in seen:
            output.append(task_id)
            seen.add(task_id)
    return output


def _authoritative_ci_policy(ci: Any) -> tuple[bool, int, str]:
    """Require recorded authoritative remote policy with at least one required check."""

    if not isinstance(ci, dict):
        return False, 0, "invalid"
    raw_checks = ci.get("checks")
    if not isinstance(raw_checks, list):
        return False, 0, "missing"
    policies = [
        item
        for item in raw_checks
        if isinstance(item, dict) and item.get("kind") == "remote_policy"
    ]
    if len(policies) != 1:
        return False, 0, "ambiguous"
    policy = policies[0]
    required = policy.get("required_checks")
    if not isinstance(required, list):
        return False, 0, str(policy.get("source") or "invalid")
    valid_required = [
        item
        for item in required
        if isinstance(item, dict) and bool(str(item.get("context") or "").strip())
    ]
    ok = policy.get("authoritative") is True and len(valid_required) == len(required) and bool(required)
    return ok, len(valid_required), str(policy.get("source") or "unknown")


def _task_bundle_checks(
    config: ProjectConfig,
    run_id: str,
    task_id: str,
) -> list[AcceptanceCheck]:
    task_dir = config.state_dir / "evidence" / run_id / task_id
    missing = [name for name in _REQUIRED_TASK_ARTIFACTS if not (task_dir / name).is_file()]
    checks = [
        _check(
            f"task:{task_id}:artifacts",
            not missing,
            "complete deterministic evidence bundle"
            if not missing
            else "missing: " + ", ".join(missing),
        )
    ]
    if missing:
        return checks

    try:
        quality = _read_json(task_dir / "quality.json")
        review = _read_json(task_dir / "review.json")
        ci = _read_json(task_dir / "ci.json")
        pr = _read_json(task_dir / "pr.json")
        risk = _read_json(task_dir / "risk.json")
        task = _read_json(task_dir / "task.json")
        patch = (task_dir / "diff.patch").read_text(encoding="utf-8")
    except (OSError, json.JSONDecodeError) as exc:
        checks.append(
            _check(
                f"task:{task_id}:parse",
                False,
                f"evidence is unreadable: {type(exc).__name__}: {exc}",
            )
        )
        return checks

    required_quality = (
        [item for item in quality if isinstance(item, dict) and item.get("required", True)]
        if isinstance(quality, list)
        else []
    )
    quality_ok = bool(required_quality) and all(item.get("ok") is True for item in required_quality)
    checks.append(
        _check(
            f"task:{task_id}:quality",
            quality_ok,
            f"{len(required_quality)} required deterministic gates; all must PASS",
        )
    )

    reviewer_map = review.get("reviewers", {}) if isinstance(review, dict) else {}
    configured_roles_ok = all(reviewer_map.get(role) == "pass" for role in config.review_roles)
    review_ok = (
        isinstance(review, dict)
        and review.get("verdict") == "pass"
        and (not config.review_roles or configured_roles_ok)
    )
    checks.append(
        _check(
            f"task:{task_id}:review",
            review_ok,
            "independent review verdict PASS"
            + (f" for roles {config.review_roles}" if config.review_roles else ""),
        )
    )

    policy_ok, required_check_count, policy_source = _authoritative_ci_policy(ci)
    ci_status = ci.get("status") if isinstance(ci, dict) else "invalid"
    ci_ok = ci_status == "pass" and policy_ok
    checks.append(
        _check(
            f"task:{task_id}:ci",
            ci_ok,
            (
                f"authoritative CI status={ci_status}; policy={policy_source}; "
                f"required_checks={required_check_count}"
            ),
        )
    )

    pr_ok = isinstance(pr, dict) and bool(pr.get("url")) and bool(pr.get("head_sha"))
    checks.append(
        _check(
            f"task:{task_id}:pr",
            pr_ok,
            "PR URL and exact candidate head SHA are present",
        )
    )

    task_ok = (
        isinstance(task, dict)
        and str(task.get("id")) == task_id
        and isinstance(task.get("requirement_ids"), list)
        and bool(task.get("requirement_ids"))
    )
    checks.append(
        _check(
            f"task:{task_id}:traceability",
            task_ok,
            "task is bound to one or more immutable requirement IDs",
        )
    )

    checks.append(
        _check(
            f"task:{task_id}:risk",
            isinstance(risk, dict),
            "deterministic risk report is present before semantic review",
        )
    )
    checks.append(
        _check(
            f"task:{task_id}:diff",
            bool(patch.strip()),
            "integrated task has a non-empty candidate diff",
        )
    )
    return checks


def _compliance_check(status: dict[str, Any]) -> AcceptanceCheck:
    values = status.get("values") if isinstance(status.get("values"), dict) else {}
    requirements = values.get("requirements") if isinstance(values, dict) else None
    compliance = values.get("compliance") if isinstance(values, dict) else None
    entries = compliance.get("entries") if isinstance(compliance, dict) else None
    if not isinstance(requirements, list) or not isinstance(entries, dict):
        return _check(
            "final_compliance",
            False,
            "terminal LangGraph compliance evidence is missing",
        )

    mandatory = [
        str(item.get("id"))
        for item in requirements
        if isinstance(item, dict)
        and item.get("id")
        and item.get("severity", "mandatory") == "mandatory"
    ]
    failing = [
        requirement_id
        for requirement_id in mandatory
        if not isinstance(entries.get(requirement_id), dict)
        or entries[requirement_id].get("status") != "pass"
    ]
    return _check(
        "final_compliance",
        bool(mandatory) and not failing,
        f"mandatory={len(mandatory)}; non-PASS={failing}",
    )


def _budget_check(config: ProjectConfig, run_id: str) -> AcceptanceCheck:
    path = config.state_dir / "evidence" / run_id / "run-budget.json"
    if not path.is_file():
        return _check("run_budget", False, "durable run budget ledger is missing")
    try:
        payload = _read_json(path)
    except (OSError, json.JSONDecodeError) as exc:
        return _check(
            "run_budget",
            False,
            f"budget ledger is unreadable: {type(exc).__name__}: {exc}",
        )
    if not isinstance(payload, dict) or payload.get("run_id") != run_id:
        return _check("run_budget", False, "budget ledger identity mismatch")
    attempts = payload.get("model_attempts_reserved")
    tokens = payload.get("estimated_tokens_reserved")
    ok = (
        isinstance(attempts, int)
        and isinstance(tokens, int)
        and 0 <= attempts <= config.run_budget.max_model_attempts
        and 0 <= tokens <= config.run_budget.max_estimated_tokens
    )
    return _check(
        "run_budget",
        ok,
        (
            f"model attempts {attempts}/{config.run_budget.max_model_attempts}; "
            f"estimated tokens {tokens}/{config.run_budget.max_estimated_tokens}"
        ),
    )


def _supervisor_checks(
    run_id: str,
    target_repository: str | None,
    evidence: ExternalSupervisorEvidence | None,
) -> list[AcceptanceCheck]:
    if evidence is None:
        return [
            _check(
                "controller_restart",
                False,
                "external supervisor evidence not supplied",
            ),
            _check(
                "exceptional_hitl",
                False,
                "external supervisor evidence not supplied",
            ),
            _check(
                "final_independent_audit",
                False,
                "external supervisor evidence not supplied",
            ),
        ]

    identity_ok = (
        evidence.run_id == run_id
        and target_repository is not None
        and evidence.target_repository.lower() == target_repository.lower()
    )
    restart = evidence.restart
    restart_ok = (
        identity_ok
        and restart.before_pid != restart.after_pid
        and restart.automatic_recovery_observed
    )
    exception = evidence.exceptional_hitl
    exception_ok = (
        identity_ok
        and exception.deliberately_injected
        and exception.kind == "risk_policy"
        and exception.action in {"approve", "edit"}
        and exception.no_manual_code_edit
    )
    required_audits = {
        "requirements",
        "architecture",
        "compatibility",
        "security",
        "evidence",
    }
    audits = {
        str(key): str(value).lower()
        for key, value in evidence.final_independent_checks.items()
    }
    audit_ok = identity_ok and all(audits.get(name) == "pass" for name in required_audits)
    return [
        _check(
            "controller_restart",
            restart_ok,
            (
                f"run identity={identity_ok}; pid {restart.before_pid}->{restart.after_pid}; "
                f"automatic recovery={restart.automatic_recovery_observed}"
            ),
        ),
        _check(
            "exceptional_hitl",
            exception_ok,
            (
                f"kind={exception.kind}; injected={exception.deliberately_injected}; "
                f"action={exception.action}; no manual code edit={exception.no_manual_code_edit}"
            ),
        ),
        _check(
            "final_independent_audit",
            audit_ok,
            "required final checks: requirements, architecture, compatibility, security, evidence",
        ),
    ]


def load_supervisor_evidence(path: Path | None) -> ExternalSupervisorEvidence | None:
    if path is None:
        return None
    try:
        return ExternalSupervisorEvidence.model_validate(_read_json(path))
    except (OSError, json.JSONDecodeError, ValidationError) as exc:
        raise ValueError(f"invalid external supervisor evidence: {exc}") from exc


def evaluate_external_acceptance(
    config: ProjectConfig,
    status: dict[str, Any],
    *,
    supervisor_evidence: ExternalSupervisorEvidence | None = None,
) -> ExternalAcceptanceReport:
    """Evaluate the general-purpose release gate from durable run/evidence artifacts.

    This verifier never calls a model and never mutates the target repository. External
    process-restart and deliberately exceptional HITL proof must come from an acceptance supervisor
    rather than being inferred from chat history or operator claims.
    """

    run_id = str(status.get("id") or "").strip()
    project_id = str(status.get("project_id") or "").strip()
    if not run_id or not project_id:
        raise ValueError("run status must contain durable id and project_id")

    target_repository = config.github_repo
    checks: list[AcceptanceCheck] = []
    checks.append(
        _check(
            "external_target_repository",
            bool(target_repository) and target_repository.lower() != _ORCHESTRATOR_REPOSITORY,
            f"configured GitHub target={target_repository or 'none'}",
        )
    )
    checks.append(
        _check(
            "terminal_convergence",
            status.get("status") == "converged" and bool(status.get("finished_at")),
            f"status={status.get('status')}; finished_at={status.get('finished_at')}",
        )
    )

    values = status.get("values") if isinstance(status.get("values"), dict) else {}
    expected_hash = values.get("requirements_hash") if isinstance(values, dict) else None
    try:
        current_hash = sha256_file(config.requirements_path)
    except OSError:
        current_hash = None
    checks.append(
        _check(
            "immutable_requirements",
            isinstance(expected_hash, str) and current_hash == expected_hash,
            f"checkpoint={expected_hash}; source={current_hash}",
        )
    )

    sandbox_ok = (
        config.sandbox.mode == "container"
        and bool(config.sandbox.image)
        and _is_digest_pinned_image(str(config.sandbox.image))
    )
    checks.append(
        _check(
            "digest_pinned_sandbox",
            sandbox_ok,
            f"mode={config.sandbox.mode}; image={config.sandbox.image or 'none'}",
        )
    )
    checks.append(_budget_check(config, run_id))

    events_path = config.state_dir / "evidence" / run_id / "events.jsonl"
    events, event_error = _read_events(events_path)
    checks.append(
        _check(
            "event_stream",
            event_error is None,
            event_error or f"valid events={len(events)}",
        )
    )
    merged_task_ids = _merged_task_ids(events) if event_error is None else []
    checks.append(
        _check(
            "multiple_autonomous_cycles",
            len(merged_task_ids) >= 2,
            f"distinct merged tasks={merged_task_ids}",
        )
    )
    for task_id in merged_task_ids:
        checks.extend(_task_bundle_checks(config, run_id, task_id))

    checks.append(_compliance_check(status))
    checks.extend(
        _supervisor_checks(
            run_id,
            target_repository,
            supervisor_evidence,
        )
    )

    return ExternalAcceptanceReport(
        run_id=run_id,
        project_id=project_id,
        target_repository=target_repository,
        ready=all(check.ok for check in checks),
        merged_task_ids=merged_task_ids,
        checks=checks,
    )
