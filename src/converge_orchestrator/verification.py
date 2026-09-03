from __future__ import annotations

import hashlib
import json
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

_CACHE_VERSION = 1


def verifier_config_sha256(config: ProjectConfig) -> str:
    """Fingerprint only deterministic verifier policy relevant to cached results."""
    payload = {
        requirement_id: [gate.model_dump(mode="json") for gate in gates]
        for requirement_id, gates in sorted(config.requirement_verifiers.items())
    }
    encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def write_baseline_verification_cache(
    config: ProjectConfig,
    *,
    base_commit: str,
    requirements_sha256: str,
    results: list[RequirementVerification],
) -> None:
    """Persist derivative verifier evidence atomically outside the target repository."""
    path = config.state_dir / "baseline-verification.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "version": _CACHE_VERSION,
        "base_commit": base_commit,
        "requirements_sha256": requirements_sha256,
        "verifier_config_sha256": verifier_config_sha256(config),
        "results": [result.model_dump(mode="json") for result in results],
    }
    temporary = path.with_suffix(".json.tmp")
    temporary.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    temporary.replace(path)


def load_baseline_verification_cache(
    config: ProjectConfig,
    *,
    base_commit: str,
    requirements_sha256: str,
) -> list[RequirementVerification] | None:
    """Load cache only when code, immutable requirements and verifier policy all match."""
    path = config.state_dir / "baseline-verification.json"
    if not path.is_file():
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(payload, dict):
            return None
        if payload.get("version") != _CACHE_VERSION:
            return None
        if payload.get("base_commit") != base_commit:
            return None
        if payload.get("requirements_sha256") != requirements_sha256:
            return None
        if payload.get("verifier_config_sha256") != verifier_config_sha256(config):
            return None
        raw_results = payload.get("results")
        if not isinstance(raw_results, list):
            return None
        return [RequirementVerification.model_validate(item) for item in raw_results]
    except (OSError, ValueError, TypeError):
        # Cache is derivative evidence. Corruption must trigger fresh deterministic execution,
        # never make a policy decision and never require HITL by itself.
        return None


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
