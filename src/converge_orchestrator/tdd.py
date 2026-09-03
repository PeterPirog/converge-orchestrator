from __future__ import annotations

import hashlib
import json
import subprocess
from pathlib import Path, PurePosixPath

from .git import changed_files, paths_within_allowlist
from .models import GateResult, ProjectConfig, QualityGate, TaskEnvelope
from .quality import effective_quality_gates
from .sandbox import ExecutionSandbox

_INVALID_EXECUTION_CODES = {124, 127}
_TEST_DIRECTORY_NAMES = {"test", "tests", "spec", "specs", "__tests__"}
_MAX_FAILURE_MARKER_LENGTH = 512


def requires_tdd(task: TaskEnvelope) -> bool:
    return task.tdd.mode == "required"


def _resolve_test_gate(
    config: ProjectConfig,
    cwd: Path,
    task: TaskEnvelope,
) -> QualityGate:
    gates = effective_quality_gates(config, cwd)
    requested = task.tdd.test_gate
    if requested:
        for gate in gates:
            if gate.name == requested:
                return gate
        raise ValueError(f"TDD requested unknown quality gate: {requested}")
    for gate in gates:
        if "test" in gate.name.lower():
            return gate
    raise ValueError(
        "TDD requires an existing deterministic test gate; plan test infrastructure first"
    )


def _execute(config: ProjectConfig, gate: QualityGate, cwd: Path) -> GateResult:
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
            required=True,
            returncode=124,
            output=output[-12000:],
        )
    except OSError as exc:
        return GateResult(
            name=gate.name,
            ok=False,
            required=True,
            returncode=127,
            output=f"Unable to execute TDD gate: {exc}"[-12000:],
        )
    return GateResult(
        name=gate.name,
        ok=result.returncode == 0,
        required=True,
        returncode=result.returncode,
        output=result.stdout[-12000:],
    )


def _file_hashes(cwd: Path, paths: list[str]) -> dict[str, str]:
    hashes: dict[str, str] = {}
    for path in paths:
        candidate = cwd / path
        if not candidate.is_file():
            continue
        digest = hashlib.sha256(candidate.read_bytes()).hexdigest()
        hashes[path] = digest
    return hashes


def _details(result: GateResult) -> dict:
    return {
        "gate": result.name,
        "gate_ok": result.ok,
        "gate_returncode": result.returncode,
        "gate_output": result.output[-8000:],
    }


def _looks_like_test_artifact(path: str) -> bool:
    """Recognize common cross-language test paths without trusting Planner-declared globs."""
    normalized = PurePosixPath(path.replace("\\", "/"))
    parts = [part.lower() for part in normalized.parts]
    if any(part in _TEST_DIRECTORY_NAMES or part.endswith(".tests") for part in parts[:-1]):
        return True

    name = normalized.name.lower()
    stem = normalized.stem.lower()
    return (
        name.startswith("test_")
        or stem.endswith("_test")
        or ".test." in name
        or ".spec." in name
        or name.endswith("test.java")
        or name.endswith("tests.java")
        or name.endswith("test.kt")
        or name.endswith("tests.kt")
        or name.endswith("test.cs")
        or name.endswith("tests.cs")
        or name.endswith("test.php")
        or name.endswith("spec.rb")
    )


def _valid_failure_marker(marker: str) -> bool:
    stripped = marker.strip()
    return (
        4 <= len(stripped) <= _MAX_FAILURE_MARKER_LENGTH
        and "\n" not in stripped
        and "\r" not in stripped
        and "\x00" not in stripped
    )


def run_tdd_baseline(
    config: ProjectConfig,
    cwd: Path,
    task: TaskEnvelope,
) -> GateResult:
    """Capture the deterministic test state before the RED test is written."""
    if not requires_tdd(task):
        return GateResult(
            name="tdd_baseline",
            ok=True,
            required=False,
            returncode=0,
            output=json.dumps({"mode": "not_applicable"}, sort_keys=True),
        )
    try:
        gate = _resolve_test_gate(config, cwd, task)
    except ValueError as exc:
        return GateResult(
            name="tdd_baseline",
            ok=False,
            required=True,
            returncode=1,
            output=json.dumps({"reason": str(exc)}, sort_keys=True),
        )
    result = _execute(config, gate, cwd)
    paths = changed_files(cwd, config.base_branch)
    usable = result.returncode not in _INVALID_EXECUTION_CODES and not paths
    details = {
        **_details(result),
        "changed_files_after_baseline": paths,
        "usable": usable,
    }
    return GateResult(
        name="tdd_baseline",
        ok=usable,
        required=True,
        returncode=0 if usable else 1,
        output=json.dumps(details, ensure_ascii=False, sort_keys=True),
    )


def run_tdd_red(
    config: ProjectConfig,
    cwd: Path,
    task: TaskEnvelope,
    baseline: GateResult | None,
) -> GateResult:
    """Accept RED only when a test-only diff creates a new, specific deterministic failure."""
    if not requires_tdd(task):
        return GateResult(
            name="tdd_red",
            ok=True,
            required=False,
            returncode=0,
            output=json.dumps({"mode": "not_applicable"}, sort_keys=True),
        )
    if baseline is None or not baseline.ok:
        return GateResult(
            name="tdd_red",
            ok=False,
            required=True,
            returncode=1,
            output=json.dumps({"reason": "usable TDD baseline evidence is missing"}),
        )
    try:
        baseline_details = json.loads(baseline.output)
        gate = _resolve_test_gate(config, cwd, task)
    except (ValueError, json.JSONDecodeError) as exc:
        return GateResult(
            name="tdd_red",
            ok=False,
            required=True,
            returncode=1,
            output=json.dumps({"reason": str(exc)}, ensure_ascii=False),
        )

    result = _execute(config, gate, cwd)
    paths = changed_files(cwd, config.base_branch)
    declared_test_paths_ok = bool(paths) and paths_within_allowlist(paths, task.tdd.test_paths)
    deterministic_test_artifacts_ok = bool(paths) and all(
        _looks_like_test_artifact(path) for path in paths
    )
    task_scope_ok = paths_within_allowlist(paths, task.allowed_paths)
    expected = task.tdd.expected_failure_pattern or ""
    marker_ok = _valid_failure_marker(expected)
    baseline_output = str(baseline_details.get("gate_output", ""))
    signal_in_red = marker_ok and expected in result.output
    signal_in_baseline = marker_ok and expected in baseline_output
    ordinary_failure = result.returncode != 0 and result.returncode not in _INVALID_EXECUTION_CODES
    novel_expected_failure = signal_in_red and not signal_in_baseline
    hashes = (
        _file_hashes(cwd, paths)
        if declared_test_paths_ok and deterministic_test_artifacts_ok and task_scope_ok
        else {}
    )
    all_changed_files_hashable = len(hashes) == len(paths)
    ok = (
        declared_test_paths_ok
        and deterministic_test_artifacts_ok
        and task_scope_ok
        and marker_ok
        and ordinary_failure
        and novel_expected_failure
        and all_changed_files_hashable
    )
    details = {
        **_details(result),
        "changed_files": paths,
        "test_paths": task.tdd.test_paths,
        "allowed_paths": task.allowed_paths,
        "test_paths_ok": declared_test_paths_ok,
        "deterministic_test_artifacts_ok": deterministic_test_artifacts_ok,
        "task_scope_ok": task_scope_ok,
        "ordinary_failure": ordinary_failure,
        "expected_failure_marker": expected,
        "failure_marker_valid": marker_ok,
        "expected_signal_in_red": signal_in_red,
        "expected_signal_in_baseline": signal_in_baseline,
        "red_test_sha256": hashes,
        "red_test_files_frozen": all_changed_files_hashable,
    }
    return GateResult(
        name="tdd_red",
        ok=ok,
        required=True,
        returncode=0 if ok else 1,
        output=json.dumps(details, ensure_ascii=False, sort_keys=True),
    )


def run_tdd_green(
    config: ProjectConfig,
    cwd: Path,
    task: TaskEnvelope,
    red: GateResult | None,
) -> GateResult:
    """Require the exact RED test artifact to remain unchanged and the same gate to pass."""
    if not requires_tdd(task):
        return GateResult(
            name="tdd_green",
            ok=True,
            required=False,
            returncode=0,
            output=json.dumps({"mode": "not_applicable"}, sort_keys=True),
        )
    if red is None or not red.ok:
        return GateResult(
            name="tdd_green",
            ok=False,
            required=True,
            returncode=1,
            output=json.dumps({"reason": "verified RED evidence is missing"}),
        )
    try:
        red_details = json.loads(red.output)
        gate = _resolve_test_gate(config, cwd, task)
    except (ValueError, json.JSONDecodeError) as exc:
        return GateResult(
            name="tdd_green",
            ok=False,
            required=True,
            returncode=1,
            output=json.dumps({"reason": str(exc)}, ensure_ascii=False),
        )

    expected_hashes = dict(red_details.get("red_test_sha256") or {})
    current_hashes = _file_hashes(cwd, sorted(expected_hashes))
    frozen_tests_unchanged = bool(expected_hashes) and current_hashes == expected_hashes
    result = _execute(config, gate, cwd)
    ok = frozen_tests_unchanged and result.ok
    details = {
        **_details(result),
        "red_test_sha256": expected_hashes,
        "current_red_test_sha256": current_hashes,
        "red_tests_unchanged": frozen_tests_unchanged,
    }
    return GateResult(
        name="tdd_green",
        ok=ok,
        required=True,
        returncode=0 if ok else 1,
        output=json.dumps(details, ensure_ascii=False, sort_keys=True),
    )
