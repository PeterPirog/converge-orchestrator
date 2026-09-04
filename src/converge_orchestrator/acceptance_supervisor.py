from __future__ import annotations

import hashlib
import json
import os
import secrets
import socket
import subprocess
import sys
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from pydantic import BaseModel, Field, ValidationError

from .acceptance import (
    ExternalAcceptanceReport,
    ExternalSupervisorEvidence,
    evaluate_external_acceptance,
)
from .config import load_config, load_run_config_snapshot
from .git import diff
from .models import ProjectConfig, ReviewResult
from .opencode import OpenCodeAdapter
from .persistence import configured_control_db_path
from .prompts import contract_excerpt
from .quality import effective_quality_gates
from .runtime_service import ScheduledRunController
from .sandbox import _is_digest_pinned_image
from .spec import compile_contract, is_read_only

_ORCHESTRATOR_REPOSITORY = "peterpirog/converge-orchestrator"
_REQUIRED_REVIEW_ROLES = {
    "architecture_reviewer",
    "correctness_reviewer",
    "security_reviewer",
}
_DEFAULT_POLL_SECONDS = 1.0
_API_START_TIMEOUT_SECONDS = 30.0


class AcceptanceSupervisorError(RuntimeError):
    """The live release scenario cannot be proven safely."""


class FinalAuditLane(BaseModel):
    area: str
    role: str
    verdict: str
    findings: list[dict[str, Any]] = Field(default_factory=list)


class FinalAuditEvidence(BaseModel):
    version: int = 1
    run_id: str
    target_repository: str
    requirements_sha256: str
    lanes: dict[str, FinalAuditLane]
    deterministic_evidence_ok: bool


class SupervisorProgress(BaseModel):
    """Crash-readable progress for the acceptance supervisor itself."""

    version: int = 1
    run_id: str
    restart_done: bool = False
    before_pid: int | None = None
    after_pid: int | None = None
    automatic_recovery_observed: bool = False
    hitl_done: bool = False
    hitl_action: str | None = None
    expected_risk_flag: str | None = None
    candidate_sha256: str | None = None
    no_manual_code_edit: bool = False


@dataclass
class _ManagedApi:
    process: subprocess.Popen[bytes]
    log_handle: Any
    base_url: str
    token: str

    def stop(self) -> None:
        if self.process.poll() is None:
            self.process.terminate()
            try:
                self.process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                self.process.kill()
                self.process.wait(timeout=10)
        self.log_handle.close()


def _atomic_json(path: Path, payload: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{secrets.token_hex(8)}.tmp")
    temporary.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    temporary.replace(path)


def _api_json(
    base_url: str,
    token: str,
    method: str,
    path: str,
    payload: dict[str, Any] | None = None,
    *,
    timeout: float = 15.0,
) -> Any:
    data = None if payload is None else json.dumps(payload).encode("utf-8")
    headers = {"Authorization": f"Bearer {token}"}
    if data is not None:
        headers["Content-Type"] = "application/json"
    request = Request(base_url + path, data=data, headers=headers, method=method)
    try:
        with urlopen(request, timeout=timeout) as response:  # noqa: S310 - loopback URL only
            raw = response.read().decode("utf-8")
    except HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        raise AcceptanceSupervisorError(
            f"Converge API {method} {path} returned HTTP {exc.code}: {detail[:1000]}"
        ) from exc
    except URLError as exc:
        raise AcceptanceSupervisorError(
            f"Converge API {method} {path} is unavailable: {exc.reason}"
        ) from exc
    return json.loads(raw) if raw else None


def _reserve_loopback_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _start_api(config: ProjectConfig, run_id_hint: str) -> _ManagedApi:
    port = _reserve_loopback_port()
    token = secrets.token_urlsafe(32)
    base_url = f"http://127.0.0.1:{port}"
    log_dir = config.state_dir / "acceptance" / run_id_hint
    log_dir.mkdir(parents=True, exist_ok=True)
    log_path = log_dir / f"controller-{int(time.time() * 1000)}.log"
    log_handle = log_path.open("ab", buffering=0)
    environment = os.environ.copy()
    environment["CONVERGE_API_TOKEN"] = token
    environment["CONVERGE_API_HOST"] = "127.0.0.1"
    environment["CONVERGE_API_PORT"] = str(port)
    process = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "uvicorn",
            "converge_orchestrator.api:app",
            "--host",
            "127.0.0.1",
            "--port",
            str(port),
        ],
        stdin=subprocess.DEVNULL,
        stdout=log_handle,
        stderr=subprocess.STDOUT,
        env=environment,
    )
    managed = _ManagedApi(process=process, log_handle=log_handle, base_url=base_url, token=token)
    deadline = time.monotonic() + _API_START_TIMEOUT_SECONDS
    while time.monotonic() < deadline:
        if process.poll() is not None:
            managed.stop()
            raise AcceptanceSupervisorError(
                f"acceptance controller exited during startup with code {process.returncode}; "
                f"see {log_path}"
            )
        try:
            request = Request(base_url + "/health", method="GET")
            with urlopen(request, timeout=1.0) as response:  # noqa: S310 - loopback URL only
                if response.status == 200:
                    return managed
        except (HTTPError, URLError, TimeoutError):
            pass
        time.sleep(0.2)
    managed.stop()
    raise AcceptanceSupervisorError(f"acceptance controller did not become healthy; see {log_path}")


def _validate_acceptance_preconditions(config: ProjectConfig) -> None:
    problems: list[str] = []
    target = (config.github_repo or "").lower()
    if not target or target == _ORCHESTRATOR_REPOSITORY:
        problems.append("github.repo must point to a repository outside Converge")
    if not config.auto_merge:
        problems.append("github.auto_merge must be true for autonomous multi-cycle convergence")
    if not config.require_spec_read_only or not is_read_only(config.requirements_path):
        problems.append("immutable requirements must be configured and physically read-only")
    if config.sandbox.mode != "container" or not config.sandbox.image:
        problems.append("acceptance requires the production container sandbox")
    elif not _is_digest_pinned_image(config.sandbox.image):
        problems.append("sandbox.image must be digest/content-address pinned")
    missing_roles = sorted(_REQUIRED_REVIEW_ROLES - set(config.review_roles))
    if missing_roles:
        problems.append(f"missing required independent review roles: {missing_roles}")
    contract = compile_contract(config.requirements_path)
    mandatory = [item for item in contract.requirements if item.severity == "mandatory"]
    if len(mandatory) < 2:
        problems.append(
            "acceptance requires at least two independently useful mandatory requirements"
        )
    required_gates = [
        gate
        for gate in effective_quality_gates(config, config.repo_path)
        if gate.required
    ]
    if not required_gates:
        problems.append(
            "acceptance target must expose at least one required deterministic quality gate"
        )
    if problems:
        raise AcceptanceSupervisorError("acceptance preflight failed: " + "; ".join(problems))


def _events(config: ProjectConfig, run_id: str) -> list[dict[str, Any]]:
    path = config.state_dir / "evidence" / run_id / "events.jsonl"
    if not path.is_file():
        return []
    records: list[dict[str, Any]] = []
    try:
        for line_number, raw in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            if not raw.strip():
                continue
            item = json.loads(raw)
            if not isinstance(item, dict) or not isinstance(item.get("event"), str):
                raise AcceptanceSupervisorError(
                    f"invalid event stream record at line {line_number}"
                )
            records.append(item)
    except (OSError, json.JSONDecodeError) as exc:
        raise AcceptanceSupervisorError(f"cannot read acceptance event stream: {exc}") from exc
    return records


def _merged_task_ids(config: ProjectConfig, run_id: str) -> list[str]:
    output: list[str] = []
    for event in _events(config, run_id):
        if event.get("event") != "merged":
            continue
        payload = event.get("payload") if isinstance(event.get("payload"), dict) else {}
        task_id = str(payload.get("task_id") or "").strip()
        if task_id and task_id not in output:
            output.append(task_id)
    return output


def _project_and_unfinished_run(
    config_path: Path,
    project_id: str,
) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
    controller = ScheduledRunController(configured_control_db_path(), restore_on_start=False)
    try:
        project = controller.registry.get_project(project_id)
    except KeyError:
        return None, None
    registered = Path(str(project["config_path"])).expanduser().resolve()
    if registered != config_path:
        raise AcceptanceSupervisorError(
            f"project {project_id} is already bound to a different config: {registered}"
        )
    unfinished = [
        record
        for record in controller.registry.runs_for_project(project_id)
        if not record.get("finished_at")
    ]
    if len(unfinished) > 1:
        raise AcceptanceSupervisorError(
            f"project {project_id} has multiple unfinished runs; acceptance identity is ambiguous"
        )
    return project, unfinished[0] if unfinished else None


def _observer() -> ScheduledRunController:
    return ScheduledRunController(configured_control_db_path(), restore_on_start=False)


def _pinned_config_for_run(
    observer: ScheduledRunController,
    run_id: str,
) -> ProjectConfig:
    record = observer.registry.get_run(run_id)
    snapshot_path = record.get("config_snapshot_path")
    snapshot_hash = record.get("config_snapshot_sha256")
    if not snapshot_path or not snapshot_hash:
        raise AcceptanceSupervisorError(
            "acceptance run is missing hash-pinned configuration metadata"
        )
    return load_run_config_snapshot(str(snapshot_path), str(snapshot_hash))


def _candidate_fingerprint(
    observer: ScheduledRunController,
    run_id: str,
) -> str:
    status = observer.status(run_id)
    values = status.get("values") if isinstance(status.get("values"), dict) else {}
    worktree = values.get("worktree")
    expected = values.get("risk_fingerprint")
    if not isinstance(worktree, str) or not worktree or not isinstance(expected, str):
        raise AcceptanceSupervisorError(
            "risk_policy checkpoint does not expose an exact worktree/candidate fingerprint"
        )
    config = _pinned_config_for_run(observer, run_id)
    patch = diff(Path(worktree), config.base_branch)
    actual = hashlib.sha256(patch.encode("utf-8")).hexdigest()
    if actual != expected:
        raise AcceptanceSupervisorError(
            "candidate changed outside the checkpointed risk decision boundary; refusing approval"
        )
    return actual


def _parse_review(role: str, output: str) -> ReviewResult:
    stripped = output.strip()
    try:
        payload = json.loads(stripped)
    except json.JSONDecodeError:
        start = stripped.find("{")
        end = stripped.rfind("}")
        if start < 0 or end <= start:
            raise AcceptanceSupervisorError(f"{role} final audit did not return JSON") from None
        payload = json.loads(stripped[start : end + 1])
    try:
        return ReviewResult.model_validate(payload)
    except ValidationError as exc:
        raise AcceptanceSupervisorError(
            f"{role} final audit returned invalid review JSON: {exc}"
        ) from exc


def _final_audit_prompt(area: str, requirements: str) -> str:
    schema = {
        "verdict": "pass|reject",
        "findings": [
            {
                "severity": "blocker|major|minor|note",
                "reason": "evidence-backed finding",
                "required_fix": "required action or null",
            }
        ],
    }
    return f"""Perform a FINAL read-only external acceptance audit of the repository.
The Builder is finished. Do not edit files. Re-read the repository and immutable requirements
from the provided authoritative excerpt. Audit only the area: {area}. Do not assume earlier
reviewers were correct. A material uncertainty, unverified mandatory requirement, architecture
drift, compatibility break or security defect relevant to this area is REJECT.
Return ONLY JSON matching: {json.dumps(schema)}

IMMUTABLE REQUIREMENTS:
{requirements}
"""


def _run_final_audit(
    config: ProjectConfig,
    run_id: str,
    status: dict[str, Any],
    supervisor: ExternalSupervisorEvidence,
) -> tuple[dict[str, str], FinalAuditEvidence]:
    contract = compile_contract(config.requirements_path)
    requirements = contract_excerpt(contract.requirements)
    lanes_spec = {
        "requirements": "architecture_reviewer",
        "architecture": "architecture_reviewer",
        "compatibility": "correctness_reviewer",
        "security": "security_reviewer",
    }
    lanes: dict[str, FinalAuditLane] = {}
    results: dict[str, str] = {}
    adapter = OpenCodeAdapter(config)
    for area, role in lanes_spec.items():
        result = adapter.invoke(role, _final_audit_prompt(area, requirements), config.repo_path)
        if not result.ok:
            review = ReviewResult(
                verdict="reject",
                findings=[
                    {
                        "severity": "major",
                        "reason": f"{role} final audit execution failed: {result.output[-2000:]}",
                        "required_fix": "final independent audit must complete successfully",
                    }
                ],
            )
        else:
            review = _parse_review(role, result.output)
        results[area] = review.verdict
        lanes[area] = FinalAuditLane(
            area=area,
            role=role,
            verdict=review.verdict,
            findings=[item.model_dump(mode="json") for item in review.findings],
        )

    provisional = supervisor.model_copy(
        update={
            "final_independent_checks": {
                "requirements": results["requirements"],
                "architecture": results["architecture"],
                "compatibility": results["compatibility"],
                "security": results["security"],
                "evidence": "pass",
            }
        }
    )
    provisional_report = evaluate_external_acceptance(
        config,
        status,
        supervisor_evidence=provisional,
    )
    deterministic_evidence_ok = all(
        check.ok
        for check in provisional_report.checks
        if check.name != "final_independent_audit"
    )
    results["evidence"] = "pass" if deterministic_evidence_ok else "reject"
    audit = FinalAuditEvidence(
        run_id=run_id,
        target_repository=str(config.github_repo),
        requirements_sha256=contract.source.sha256,
        lanes=lanes,
        deterministic_evidence_ok=deterministic_evidence_ok,
    )
    return results, audit


def _wait_for_first_merge(
    api: _ManagedApi,
    config: ProjectConfig,
    run_id: str,
    deadline: float,
    poll_seconds: float,
) -> None:
    while time.monotonic() < deadline:
        if _merged_task_ids(config, run_id):
            return
        status = _api_json(api.base_url, api.token, "GET", f"/runs/{run_id}")
        if status.get("finished_at"):
            raise AcceptanceSupervisorError("run finished before the required first merged task")
        interrupt = status.get("interrupt")
        if interrupt and interrupt.get("kind") != "ci_wait":
            raise AcceptanceSupervisorError(
                "acceptance scenario reached human intervention before its first merged task"
            )
        time.sleep(poll_seconds)
    raise AcceptanceSupervisorError("timed out waiting for the first merged task")


def _wait_for_automatic_recovery(
    api: _ManagedApi,
    config: ProjectConfig,
    run_id: str,
    event_count_before: int,
    deadline: float,
    poll_seconds: float,
) -> None:
    while time.monotonic() < deadline:
        status = _api_json(api.base_url, api.token, "GET", f"/runs/{run_id}")
        if status.get("worker_alive") is True or len(_events(config, run_id)) > event_count_before:
            return
        if status.get("finished_at"):
            raise AcceptanceSupervisorError(
                "run finished before restart recovery could be observed"
            )
        time.sleep(poll_seconds)
    raise AcceptanceSupervisorError(
        "automatic same-run recovery was not observed after restart"
    )


def _wait_for_risk_interrupt(
    api: _ManagedApi,
    run_id: str,
    expected_risk_flag: str,
    deadline: float,
    poll_seconds: float,
) -> dict[str, Any]:
    while time.monotonic() < deadline:
        status = _api_json(api.base_url, api.token, "GET", f"/runs/{run_id}")
        interrupt = status.get("interrupt")
        if interrupt:
            kind = interrupt.get("kind")
            if kind == "ci_wait":
                time.sleep(poll_seconds)
                continue
            if kind != "risk_policy":
                raise AcceptanceSupervisorError(
                    f"unexpected human interrupt during acceptance: {kind}"
                )
            flags = set(interrupt.get("risk_flags") or [])
            if expected_risk_flag not in flags:
                raise AcceptanceSupervisorError(
                    "risk_policy interrupt does not contain the predeclared injected risk flag "
                    f"{expected_risk_flag!r}"
                )
            return interrupt
        if status.get("finished_at"):
            raise AcceptanceSupervisorError(
                "run converged without the deliberately injected exceptional risk_policy HITL"
            )
        time.sleep(poll_seconds)
    raise AcceptanceSupervisorError("timed out waiting for the expected risk_policy interrupt")


def _wait_for_convergence(
    api: _ManagedApi,
    run_id: str,
    deadline: float,
    poll_seconds: float,
) -> dict[str, Any]:
    while time.monotonic() < deadline:
        status = _api_json(api.base_url, api.token, "GET", f"/runs/{run_id}")
        if status.get("finished_at"):
            if status.get("status") != "converged":
                raise AcceptanceSupervisorError(
                    f"acceptance run terminated without convergence: {status.get('status')}"
                )
            return status
        interrupt = status.get("interrupt")
        if interrupt and interrupt.get("kind") != "ci_wait":
            raise AcceptanceSupervisorError(
                f"unexpected additional HITL after injected exception: {interrupt.get('kind')}"
            )
        time.sleep(poll_seconds)
    raise AcceptanceSupervisorError("timed out waiting for terminal convergence")


def supervise_external_acceptance(
    config_path: Path,
    *,
    project_id: str,
    expected_risk_flag: str,
    output_path: Path,
    decision_provider: Callable[[dict[str, Any]], str],
    poll_seconds: float = _DEFAULT_POLL_SECONDS,
) -> ExternalAcceptanceReport:
    """Execute and prove the live external-repository release scenario.

    The supervisor is outside LangGraph. It starts the normal API controller and observes durable
    evidence. It kills/restarts that controller once, routes exactly one predeclared exceptional
    risk decision through the public API, verifies the candidate stayed unchanged while awaiting
    the operator, and finally runs fresh read-only independent audits. It never writes target code.
    """

    if poll_seconds <= 0:
        raise ValueError("poll_seconds must be positive")
    resolved_config = config_path.expanduser().resolve()
    config = load_config(resolved_config)
    _validate_acceptance_preconditions(config)
    expected_risk_flag = expected_risk_flag.strip()
    if not expected_risk_flag:
        raise ValueError("expected_risk_flag must not be empty")

    existing_project, unfinished = _project_and_unfinished_run(resolved_config, project_id)
    run_id_hint = str(unfinished["id"]) if unfinished else f"new-{project_id}"
    api = _start_api(config, run_id_hint)
    progress: SupervisorProgress | None = None
    observer: ScheduledRunController | None = None
    try:
        if existing_project is None:
            _api_json(
                api.base_url,
                api.token,
                "POST",
                "/projects",
                {"id": project_id, "config_path": str(resolved_config)},
            )
        if unfinished is None:
            record = _api_json(
                api.base_url,
                api.token,
                "POST",
                f"/projects/{project_id}/run",
            )
            run_id = str(record["id"])
        else:
            run_id = str(unfinished["id"])

        pinned = _pinned_config_for_run(_observer(), run_id)
        deadline = time.monotonic() + pinned.run_budget.max_wall_time_seconds + 60
        progress_path = pinned.state_dir / "acceptance" / run_id / "supervisor-progress.json"
        if progress_path.is_file():
            try:
                progress = SupervisorProgress.model_validate_json(
                    progress_path.read_text(encoding="utf-8")
                )
            except (OSError, ValidationError) as exc:
                raise AcceptanceSupervisorError(
                    f"invalid supervisor progress journal: {exc}"
                ) from exc
            if progress.run_id != run_id:
                raise AcceptanceSupervisorError("supervisor progress journal run identity mismatch")
        else:
            progress = SupervisorProgress(run_id=run_id, expected_risk_flag=expected_risk_flag)
            _atomic_json(progress_path, progress.model_dump(mode="json"))
        if progress.expected_risk_flag != expected_risk_flag:
            raise AcceptanceSupervisorError(
                "existing supervisor journal was created for a different expected risk flag"
            )

        observer = _observer()
        if not progress.restart_done:
            _wait_for_first_merge(api, pinned, run_id, deadline, poll_seconds)
            event_count_before = len(_events(pinned, run_id))
            before_pid = api.process.pid
            api.stop()
            api = _start_api(pinned, run_id)
            after_pid = api.process.pid
            if before_pid == after_pid:
                raise AcceptanceSupervisorError(
                    "controller restart did not change process identity"
                )
            progress = progress.model_copy(
                update={
                    "restart_done": True,
                    "before_pid": before_pid,
                    "after_pid": after_pid,
                }
            )
            _atomic_json(progress_path, progress.model_dump(mode="json"))
            _wait_for_automatic_recovery(
                api,
                pinned,
                run_id,
                event_count_before,
                deadline,
                poll_seconds,
            )
            progress = progress.model_copy(update={"automatic_recovery_observed": True})
            _atomic_json(progress_path, progress.model_dump(mode="json"))
        elif not progress.automatic_recovery_observed:
            raise AcceptanceSupervisorError(
                "supervisor journal records a restart without proven automatic recovery"
            )

        if not progress.hitl_done:
            interrupt = _wait_for_risk_interrupt(
                api,
                run_id,
                expected_risk_flag,
                deadline,
                poll_seconds,
            )
            before = _candidate_fingerprint(observer, run_id)
            action = decision_provider(interrupt)
            if action != "approve":
                raise AcceptanceSupervisorError(
                    "external acceptance requires the injected risk to be explicitly approved; "
                    f"operator returned {action!r}"
                )
            after = _candidate_fingerprint(observer, run_id)
            no_manual_edit = before == after
            if not no_manual_edit:
                raise AcceptanceSupervisorError(
                    "candidate changed while the exceptional HITL decision was pending"
                )
            _api_json(
                api.base_url,
                api.token,
                "POST",
                f"/runs/{run_id}/decision",
                {"action": "approve"},
            )
            progress = progress.model_copy(
                update={
                    "hitl_done": True,
                    "hitl_action": "approve",
                    "candidate_sha256": before,
                    "no_manual_code_edit": True,
                }
            )
            _atomic_json(progress_path, progress.model_dump(mode="json"))
        elif not progress.no_manual_code_edit:
            raise AcceptanceSupervisorError(
                "supervisor journal does not prove an unchanged candidate during HITL"
            )

        _wait_for_convergence(api, run_id, deadline, poll_seconds)
        api.stop()
        observer = _observer()
        terminal = observer.status(run_id)

        base_supervisor = ExternalSupervisorEvidence.model_validate(
            {
                "run_id": run_id,
                "target_repository": pinned.github_repo,
                "restart": {
                    "before_pid": progress.before_pid,
                    "after_pid": progress.after_pid,
                    "automatic_recovery_observed": progress.automatic_recovery_observed,
                },
                "exceptional_hitl": {
                    "kind": "risk_policy",
                    "expected_risk_flag": expected_risk_flag,
                    "deliberately_injected": True,
                    "action": progress.hitl_action,
                    "no_manual_code_edit": progress.no_manual_code_edit,
                },
                "final_independent_checks": {
                    "requirements": "reject",
                    "architecture": "reject",
                    "compatibility": "reject",
                    "security": "reject",
                    "evidence": "reject",
                },
            }
        )
        checks, audit = _run_final_audit(pinned, run_id, terminal, base_supervisor)
        supervisor = base_supervisor.model_copy(update={"final_independent_checks": checks})
        _atomic_json(output_path, supervisor.model_dump(mode="json"))
        audit_path = pinned.state_dir / "evidence" / run_id / "external-final-audit.json"
        _atomic_json(audit_path, audit.model_dump(mode="json"))

        report = evaluate_external_acceptance(
            pinned,
            terminal,
            supervisor_evidence=supervisor,
        )
        report_path = pinned.state_dir / "evidence" / run_id / "external-acceptance-report.json"
        _atomic_json(report_path, report.model_dump(mode="json"))
        return report
    finally:
        if api.process.poll() is None:
            api.stop()
