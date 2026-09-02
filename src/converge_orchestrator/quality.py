from __future__ import annotations

import json
import shlex
import subprocess
from pathlib import Path

from .compliance import ComplianceEngine
from .git import changed_files, diff_line_count, paths_within_allowlist
from .inspector import inspect_repository
from .models import GateResult, ProjectConfig, QualityGate, RequirementStatus, TaskEnvelope
from .shell import run_configured
from .spec import compile_contract
from .verification import run_requirement_verifiers


def _command_key(gate: QualityGate) -> tuple[str, ...]:
    if isinstance(gate.command, list):
        command = tuple(gate.command)
    elif gate.shell:
        command = ("<shell>", gate.command)
    else:
        command = tuple(shlex.split(gate.command))
    return command


def effective_quality_gates(config: ProjectConfig, cwd: Path) -> list[QualityGate]:
    """Return explicit policy plus conservative stack discovery without exact duplicates."""
    gates = list(config.quality_gates)
    if not config.auto_discover_quality:
        return gates
    known_commands = {_command_key(gate) for gate in gates}
    for discovered in inspect_repository(cwd).quality_gates:
        command_key = _command_key(discovered)
        if command_key in known_commands:
            continue
        gates.append(discovered)
        known_commands.add(command_key)
    return gates


def _execute_quality_gate(gate: QualityGate, cwd: Path) -> GateResult:
    try:
        result = run_configured(
            gate.command,
            cwd=cwd,
            timeout=gate.timeout_seconds,
            shell=gate.shell,
        )
    except subprocess.TimeoutExpired as exc:
        output = f"Gate timed out after {gate.timeout_seconds}s"
        if exc.stdout:
            output = f"{output}\n{exc.stdout}"
        return GateResult(
            name=gate.name,
            ok=False,
            required=gate.required,
            returncode=124,
            output=output[-12000:],
        )
    except OSError as exc:
        return GateResult(
            name=gate.name,
            ok=False,
            required=gate.required,
            returncode=127,
            output=f"Unable to execute quality gate: {exc}"[-12000:],
        )
    return GateResult(
        name=gate.name,
        ok=result.returncode == 0,
        required=gate.required,
        returncode=result.returncode,
        output=result.stdout[-12000:],
    )


def run_quality_gates(config: ProjectConfig, cwd: Path) -> list[GateResult]:
    return [_execute_quality_gate(gate, cwd) for gate in effective_quality_gates(config, cwd)]


def _convergence_details(
    config: ProjectConfig,
    cwd: Path,
    task: TaskEnvelope,
) -> tuple[bool, dict]:
    if not config.requirement_verifiers:
        return True, {
            "mode": "semantic_review_fallback",
            "mandatory_regressions": 0,
            "target_progress_required": False,
        }

    contract = compile_contract(config.requirements_path)
    baseline_results = run_requirement_verifiers(config, config.repo_path, contract.requirements)
    candidate_results = run_requirement_verifiers(config, cwd, contract.requirements)
    baseline = ComplianceEngine.apply_verifications(
        ComplianceEngine.initial(contract),
        baseline_results,
    )
    candidate = ComplianceEngine.apply_verifications(
        ComplianceEngine.initial(contract),
        candidate_results,
    )
    regressions = ComplianceEngine.compare_mandatory_regressions(
        baseline,
        candidate,
        contract.requirements,
    )

    baseline_status = {
        result.requirement_id: result.status.value for result in baseline_results
    }
    candidate_status = {
        result.requirement_id: result.status.value for result in candidate_results
    }
    targeted = [
        requirement_id
        for requirement_id in task.requirement_ids
        if config.requirement_verifiers.get(requirement_id)
    ]
    improved = [
        requirement_id
        for requirement_id in targeted
        if baseline_status.get(requirement_id) != RequirementStatus.PASS.value
        and candidate_status.get(requirement_id) == RequirementStatus.PASS.value
    ]
    target_progress_ok = not targeted or bool(improved)
    evidence = {
        result.requirement_id: result.evidence
        for result in candidate_results
        if result.evidence
    }
    details = {
        "mode": "deterministic_requirement_verifiers",
        "mandatory_regressions": regressions,
        "targeted_configured": targeted,
        "target_improved": improved,
        "target_progress_required": bool(targeted),
        "baseline": baseline_status,
        "candidate": candidate_status,
        "evidence": evidence,
    }
    return regressions == 0 and target_progress_ok, details


def run_scope_gate(
    config: ProjectConfig,
    cwd: Path,
    task: TaskEnvelope,
) -> GateResult:
    paths = changed_files(cwd, config.base_branch)
    line_count = diff_line_count(cwd, config.base_branch)
    hard_limit = min(task.max_diff_lines or config.max_diff_lines_hard, config.max_diff_lines_hard)
    paths_ok = paths_within_allowlist(paths, task.allowed_paths)
    size_ok = line_count <= hard_limit
    convergence_ok, convergence = _convergence_details(config, cwd, task)
    details = {
        "changed_files": paths,
        "diff_lines": line_count,
        "hard_limit": hard_limit,
        "allowed_paths": task.allowed_paths,
        "convergence": convergence,
    }
    ok = paths_ok and size_ok and convergence_ok
    return GateResult(
        name="diff_scope",
        ok=ok,
        required=True,
        returncode=0 if ok else 1,
        output=json.dumps(details, ensure_ascii=False, sort_keys=True),
    )


def required_gates_pass(results: list[GateResult]) -> bool:
    return all(item.ok for item in results if item.required)
