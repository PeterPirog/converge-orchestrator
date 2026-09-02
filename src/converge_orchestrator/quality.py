from pathlib import Path

from .models import GateResult, ProjectConfig
from .shell import run_shell


def run_quality_gates(config: ProjectConfig, cwd: Path) -> list[GateResult]:
    results = []
    for gate in config.quality_gates:
        result = run_shell(gate.command, cwd=cwd, timeout=gate.timeout_seconds)
        results.append(GateResult(name=gate.name, ok=result.returncode == 0, required=gate.required, returncode=result.returncode, output=result.stdout[-12000:]))
    return results


def required_gates_pass(results: list[GateResult]) -> bool:
    return all(item.ok for item in results if item.required)
