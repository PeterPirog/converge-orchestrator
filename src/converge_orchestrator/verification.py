from __future__ import annotations

import subprocess
from pathlib import Path

from .models import (
    GateResult,
    ProjectConfig,
    Requirement,
    RequirementStatus,
    RequirementVerification,
)
from .sandbox import ExecutionSandbox


def _run_gate(
    config: ProjectConfig,
    requirement_id: str,
    gate_name: str,
    command: str | list[str],
    *,
    cwd: Path,
    timeout: int,
    shell: bool,
    required: bool,
    writable_cwd: bool,
) -> GateResult:
    name = f"requirement:{requirement_id}:{gate_name}"
    try:
        result = ExecutionSandbox(config).run(
            command,
            cwd=cwd,
            timeout=timeout,
            shell=shell,
            scope="quality",
            writable_cwd=writable_cwd,
        )
    except subprocess.TimeoutExpired as exc:
        output = f"Verifier timed out after {timeout}s"
        if exc.stdout:
            output = f"{output}\n{exc.stdout}"
        return GateResult(
            name=name,
            ok=False,
            required=required,
            returncode=124,
            output=output[-12000:],
        )
    except OSError as exc:
        return GateResult(
            name=name,
            ok=False,
            required=required,
            returncode=127,
            output=f"Unable to execute verifier: {exc}"[-12000:],
        )
    return GateResult(
        name=name,
        ok=result.returncode == 0,
        required=required,
        returncode=result.returncode,
        output=result.stdout[-12000:],
    )


def run_requirement_verifiers(
    config: ProjectConfig,
    cwd: Path,
    requirements: list[Requirement],
    *,
    writable_cwd: bool = True,
) -> list[RequirementVerification]:
    """Evaluate configured deterministic evidence without inventing missing verifiers."""
    results: list[RequirementVerification] = []
    known_ids = {item.id for item in requirements}
    unknown = set(config.requirement_verifiers) - known_ids
    if unknown:
        message = f"Verifier configuration references unknown requirements: {sorted(unknown)}"
        raise ValueError(message)

    for requirement in requirements:
        rules = config.requirement_verifiers.get(requirement.id, [])
        if not rules:
            results.append(
                RequirementVerification(
                    requirement_id=requirement.id,
                    status=RequirementStatus.UNVERIFIED,
                )
            )
            continue

        gates = [
            _run_gate(
                config,
                requirement.id,
                rule.name,
                rule.command,
                cwd=cwd,
                timeout=rule.timeout_seconds,
                shell=rule.shell,
                required=rule.required,
                writable_cwd=writable_cwd,
            )
            for rule in rules
        ]
        required_failures = [gate for gate in gates if gate.required and not gate.ok]
        status = RequirementStatus.FAIL if required_failures else RequirementStatus.PASS
        evidence = [
            f"{gate.name}:exit={gate.returncode}:{'PASS' if gate.ok else 'FAIL'}"
            for gate in gates
        ]
        results.append(
            RequirementVerification(
                requirement_id=requirement.id,
                status=status,
                evidence=evidence,
                gates=gates,
            )
        )
    return results


def verifier_gates(results: list[RequirementVerification]) -> list[GateResult]:
    return [gate for result in results for gate in result.gates]
