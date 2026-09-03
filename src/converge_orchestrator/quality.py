from __future__ import annotations

import json
import shlex
import subprocess
from pathlib import Path

from .architecture import run_architecture_gate
from .compliance import ComplianceEngine
from .git import GitError, changed_files, current_head, diff_line_count, paths_within_allowlist
from .inspector import inspect_repository
from .models import GateResult, ProjectConfig, QualityGate, RequirementStatus, TaskEnvelope
from .sandbox import ExecutionSandbox
from .spec import compile_contract
from .verification import (
    load_baseline_verification_cache,
    run_requirement_verifiers,
    write_baseline_verification_cache,
)


def _command_key(gate: QualityGate) -> tuple[str, ...]:
    if isinstance(gate.command, list):
        command = tuple(gate.command)
    elif gate.shell:
        command = ("<shell>", gate.command)
    else:
        command = tuple(shlex.split(gate.command))
    return command


def _gate_kind(name: str) -> str | None:
    lowered = name.lower()
    for kind in ("typecheck", "lint", "test", "build"):
        if kind in lowered:
            return kind
    return None


def effective_quality_gates(config: ProjectConfig, cwd: Path) -> list[QualityGate]:
    """Return explicit policy plus conservative discovery; explicit categories win."""
    gates = list(config.quality_gates)
    if not config.auto_discover_quality:
        return gates
    known_commands = {_command_key(gate) for gate in gates}
    explicit_kinds = {_gate_kind(gate.name) for gate in gates}
    explicit_kinds.discard(None)
    for discovered in inspect_repository(cwd).quality_gates:
        command_key = _command_key(discovered)
        if command_key in known_commands or _gate_kind(discovered.name) in explicit_kinds:
            continue
        gates.append(discovered)
        known_commands.add(command_key)
    return gates


def _execute_quality_gate(
    config: ProjectConfig,
    gate: QualityGate,
    cwd: Path,
) -> GateResult:
    try:
        result = ExecutionSandbox(config).run(
            gate.command,
            cwd=cwd,
            timeout=gate.timeout_seconds,
            shell=gate.shell,
            scope="quality",
            writable_cwd=True,
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
    results = [
        _execute_quality_gate(config, gate, cwd)
        for gate in effective_quality_gates(config, cwd)
    ]
    architecture = run_architecture_gate(config, cwd)
    if architecture is not None:
        results.append(architecture)
    return results


def _baseline_requirement_verifiers(config: ProjectConfig, contract):  # type: ignore[no-untyped-def]
    try:
        base_commit = current_head(config.repo_path)
    except GitError:
        # Some embedders/tests use a filesystem baseline without Git metadata. Cache identity cannot
        # be proven there, so execute deterministic evidence normally instead of guessing a key.
        return (
            run_requirement_verifiers(
                config,
                config.repo_path,
                contract.requirements,
                writable_cwd=False,
            ),
            False,
        )
    cached = load_baseline_verification_cache(
        config,
        base_commit=base_commit,
        requirements_sha256=contract.source.sha256,
    )
    if cached is not None:
        return cached, True
    results = run_requirement_verifiers(
        config,
        config.repo_path,
        contract.requirements,
        writable_cwd=False,
    )
    write_baseline_verification_cache(
        config,
        base_commit=base_commit,
        requirements_sha256=contract.source.sha256,
        results=results,
    )
    return results, False


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
    baseline_results, baseline_cache_hit = _baseline_requirement_verifiers(config, contract)
    candidate_results = run_requirement_verifiers(
        config,
        cwd,
        contract.requirements,
        writable_cwd=True,
    )
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
        "baseline_cache_hit": baseline_cache_hit,
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
    # Repo-controlled verifiers run first. Scope is measured only after they finish so a verifier
    # cannot mutate the candidate after allowlist/diff evidence has already been accepted.
    convergence_ok, convergence = _convergence_details(config, cwd, task)
    paths = changed_files(cwd, config.base_branch)
    line_count = diff_line_count(cwd, config.base_branch)
    hard_limit = min(
        task.max_diff_lines or config.max_diff_lines_hard,
        config.max_diff_lines_hard,
    )
    paths_ok = paths_within_allowlist(paths, task.allowed_paths)
    size_ok = line_count <= hard_limit
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
