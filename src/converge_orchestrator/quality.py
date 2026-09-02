from pathlib import Path

from .git import changed_files, diff_line_count, paths_within_allowlist
from .models import GateResult, ProjectConfig, TaskEnvelope
from .shell import run_configured


def run_quality_gates(config: ProjectConfig, cwd: Path) -> list[GateResult]:
    results: list[GateResult] = []
    for gate in config.quality_gates:
        result = run_configured(
            gate.command,
            cwd=cwd,
            timeout=gate.timeout_seconds,
            shell=gate.shell,
        )
        results.append(
            GateResult(
                name=gate.name,
                ok=result.returncode == 0,
                required=gate.required,
                returncode=result.returncode,
                output=result.stdout[-12000:],
            )
        )
    return results


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
    details = {
        "changed_files": paths,
        "diff_lines": line_count,
        "hard_limit": hard_limit,
        "allowed_paths": task.allowed_paths,
    }
    return GateResult(
        name="diff_scope",
        ok=paths_ok and size_ok,
        required=True,
        returncode=0 if paths_ok and size_ok else 1,
        output=str(details),
    )


def required_gates_pass(results: list[GateResult]) -> bool:
    return all(item.ok for item in results if item.required)
