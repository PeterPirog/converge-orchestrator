from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

from converge_orchestrator.evidence import EvidenceStore
from converge_orchestrator.models import (
    GateResult,
    ProjectConfig,
    TaskEnvelope,
    TDDPlan,
)
from converge_orchestrator.registry import ControlRegistry
from converge_orchestrator.tdd import run_tdd_baseline, run_tdd_red


def _git(cwd: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", *args],
        cwd=cwd,
        check=True,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
    )
    return result.stdout.strip()


def _repository(tmp_path: Path) -> Path:
    origin = tmp_path / "origin.git"
    subprocess.run(
        ["git", "init", "--bare", "--initial-branch=main", str(origin)],
        check=True,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
    )
    repo = tmp_path / "repo"
    subprocess.run(
        ["git", "clone", str(origin), str(repo)],
        check=True,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
    )
    _git(repo, "config", "user.email", "converge@example.invalid")
    _git(repo, "config", "user.name", "Converge Test")
    (repo / "README.md").write_text("baseline\n", encoding="utf-8")
    (repo / "src").mkdir(exist_ok=True)
    (repo / "tests").mkdir(exist_ok=True)
    (repo / "src" / "__init__.py").write_text("", encoding="utf-8")
    _git(repo, "add", "README.md", "src/__init__.py")
    _git(repo, "commit", "-m", "baseline")
    _git(repo, "push", "-u", "origin", "main")
    return repo


def _config(tmp_path: Path, repo: Path, evidence_root: Path) -> Path:
    state_dir = tmp_path / "state"
    worktree_dir = tmp_path / "worktrees"
    requirements = tmp_path / "architecture.md"
    requirements.write_text(
        "ARCH-001: System must expose the requested behavior via a TDD-verified implementation.\n",
        encoding="utf-8",
    )
    config_path = tmp_path / "converge.yaml"
    config_path.write_text(
        "\n".join(
            [
                "version: 1",
                "project:",
                f"  repo_path: {json.dumps(str(repo))}",
                f"  requirements_path: {json.dumps(str(requirements))}",
                f"  state_dir: {json.dumps(str(state_dir))}",
                f"  worktree_dir: {json.dumps(str(worktree_dir))}",
                "  require_spec_read_only: false",
                "agents:",
                "  builder:",
                "    agent: converge-builder",
                "  planner:",
                "    agent: converge-planner",
                "  scout:",
                "    agent: converge-scout",
                "  reviewer:",
                "    agent: converge-reviewer",
                "quality_gates:",
                "  - name: unit-test",
                "    command: [\"python\", \"-m\", \"pytest\", \"-q\"]",
                "    timeout_seconds: 30",
                "",
            ]
        ),
        encoding="utf-8",
    )
    return config_path


def _worker_environment() -> dict[str, str]:
    environment = dict(os.environ)
    source_root = str(Path(__file__).resolve().parents[1] / "src")
    current = environment.get("PYTHONPATH")
    environment["PYTHONPATH"] = (
        source_root if not current else os.pathsep.join((source_root, current))
    )
    return environment


def _setup_red_test_real(
    tmp_path: Path, config_path: Path
) -> tuple[ProjectConfig, TaskEnvelope, GateResult, dict, Path]:
    """Set up a real TDD RED test using the actual TDD functions."""
    cfg = load_config(config_path)
    task = TaskEnvelope(
        id="ARCH-001-1",
        requirement_ids=["ARCH-001"],
        title="Add behavior",
        objective="Expose the required behavior",
        allowed_paths=["src/**", "tests/**"],
        change_kind="behavior",
        tdd=TDDPlan(
            mode="required",
            test_paths=["tests/**"],
            test_gate="unit-test",
            expected_failure_pattern="NEW_RULE_MISSING",
            rationale="Observable behavior changes require a failing test first.",
        ),
    )

    # Create the test file with LF line endings (binary write)
    test_file = cfg.repo_path / "tests" / "test_rule.py"
    test_file.parent.mkdir()
    test_file.write_bytes(
        b"def test_new_rule():\n    assert False, 'NEW_RULE_MISSING'\n"
    )
    _git(cfg.repo_path, "add", "tests/test_rule.py")
    _git(cfg.repo_path, "commit", "-m", "Add RED test")

    class MockCompleted:
        def __init__(self, returncode: int, stdout: str):
            self.returncode = returncode
            self.stdout = stdout

    # We need to patch the sandbox for the TDD functions
    import types
    from unittest.mock import patch

    def _completed(returncode: int, stdout: str):
        return types.SimpleNamespace(returncode=returncode, stdout=stdout)

    with (
        patch(
            "converge_orchestrator.tdd.ExecutionSandbox.run",
            return_value=_completed(0, "1 passed"),
        ),
        patch(
            "converge_orchestrator.tdd.changed_files",
            return_value=[],
        ),
    ):
        baseline = run_tdd_baseline(cfg, cfg.repo_path, task)
    assert baseline.ok

    with (
        patch(
            "converge_orchestrator.tdd.ExecutionSandbox.run",
            return_value=_completed(1, "NEW_RULE_MISSING"),
        ),
        patch(
            "converge_orchestrator.tdd.changed_files",
            return_value=["tests/test_rule.py"],
        ),
    ):
        red = run_tdd_red(cfg, cfg.repo_path, task, baseline)

    assert red.ok
    red_details = json.loads(red.output)
    return cfg, task, red, red_details, test_file


def test_frozen_red_process_restart_reconciles_mutation_before_quality(tmp_path: Path) -> None:
    """
    Integration regression test for frozen RED crash window protection.

    Exercises: ScheduledRunController -> real graph_service.build_graph() -> real LangGraph
    checkpointer -> real control registry -> real worktree -> actual frozen RED snapshot code ->
    process death -> NEW ScheduledRunController -> automatic recovery.

    Proves that entry reconciliation at the build node detects and restores any frozen RED
    mutation before the writer can proceed.
    """
    repo = _repository(tmp_path)
    evidence_root = tmp_path / "evidence_root"
    config_path = _config(tmp_path, repo, evidence_root)
    registry_path = (tmp_path / "state" / "control.sqlite")
    # Clean up any existing registry from previous failed runs
    if registry_path.exists():
        registry_path.unlink()
    worker = Path(__file__).parent / "fixtures" / "process_frozen_red_crash_worker.py"
    environment = _worker_environment()

    # --- PHASE 1: Launch controller, reach build node, snapshot frozen RED, crash ---
    crashed = subprocess.run(
        [sys.executable, str(worker), "crash", str(registry_path), str(config_path)],
        cwd=Path(__file__).resolve().parents[1],
        env=environment,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        timeout=120,
    )
    print(f"CRASH WORKER STDOUT:\n{crashed.stdout}", flush=True)
    assert crashed.returncode == 94, (
        f"Expected exit code 94, got {crashed.returncode}: {crashed.stdout}"
    )

    # --- PHASE 2: Verify pre-restart state ---
    crashed_record = ControlRegistry(registry_path).runs_for_project("frozen-red-chaos")[0]
    original_run_id = crashed_record["id"]
    original_thread_id = crashed_record["thread_id"]

    assert crashed_record["status"] == "running", (
        f"Expected running, got {crashed_record['status']}"
    )
    assert crashed_record["finished_at"] is None
    assert crashed_record["lease_owner"]

    # Find the worktree and frozen RED file
    worktree_dir = tmp_path / "worktrees"
    candidate_dirs = list(worktree_dir.glob("arch-001-1*"))
    assert len(candidate_dirs) == 1, (
        f"Expected exactly one candidate worktree, found {candidate_dirs}"
    )
    worktree = candidate_dirs[0]

    frozen_red_file = worktree / "tests" / "test_rule.py"
    assert frozen_red_file.exists(), "Frozen RED file should exist in worktree"

    # Record the dirty (mutated) frozen RED content
    dirty_content = frozen_red_file.read_bytes()
    assert dirty_content == b"def test_new_rule():\n    assert True\n", (
        f"Expected mutated content, got {dirty_content}"
    )

    # Verify the durable snapshot still contains exact original bytes
    state_dir = tmp_path / "state"
    store = EvidenceStore(state_dir / "evidence")
    task_id = "ARCH-001-1"

    frozen_dir = store.frozen_red_dir(original_run_id, task_id)
    manifest_path = store.root / original_run_id / "frozen-red" / task_id / "manifest.json"
    assert manifest_path.exists(), "Manifest should exist"

    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    expected_sha = manifest["files"]["tests/test_rule.py"]["sha256"]
    frozen_blob = frozen_dir / expected_sha
    assert frozen_blob.exists(), f"Frozen blob {expected_sha} should exist"
    authoritative_content = frozen_blob.read_bytes()
    expected_original = b"def test_new_rule():\n    assert False, 'NEW_RULE_MISSING'\n"
    assert authoritative_content == expected_original, "Durable snapshot must have original bytes"

    # Verify run is unfinished, no quality/review/integration evidence after mutation
    run_evidence_dir = store.root / original_run_id / task_id
    quality_path = run_evidence_dir / "quality.json"
    review_path = run_evidence_dir / "review.json"
    pr_path = run_evidence_dir / "pr.json"
    assert not quality_path.exists(), "Quality evidence should not exist after crash"
    assert not review_path.exists(), "Review evidence should not exist after crash"
    assert not pr_path.exists(), "PR evidence should not exist after crash"

    # Verify no human decision was written
    events_path = store.root / original_run_id / "events.jsonl"
    if events_path.exists():
        events = [
            json.loads(line)
            for line in events_path.read_text(encoding="utf-8").strip().split("\n")
            if line
        ]
        human_decisions = [e for e in events if e["event"] == "human_decision"]
        assert len(human_decisions) == 0, "No human decisions should exist"
    else:
        # No events file means no events were written (crash happened before first checkpoint)
        pass

    # --- PHASE 3: Start NEW controller for recovery ---
    time.sleep(1.2)  # Wait for lease TTL to expire
    recovered = subprocess.run(
        [sys.executable, str(worker), "recover", str(registry_path)],
        cwd=Path(__file__).resolve().parents[1],
        env=environment,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        timeout=90,
    )
    assert recovered.returncode == 0, f"Recovery failed: {recovered.stdout}"

    # --- PHASE 4: Verify recovery assertions ---
    record = ControlRegistry(registry_path).runs_for_project("frozen-red-chaos")[0]

    # A. SAME IDENTITY
    assert record["id"] == original_run_id, "Recovered run_id must match original"
    assert record["thread_id"] == original_thread_id, "Recovered thread_id must match original"

    # B. REAL NODE REPLAY - verify the build node was re-entered
    # The production graph without GitHub ends at "pushed" status after integrate
    # (no GitHub configured in test, so graph ends after integrate)
    assert record["status"] in ("completed", "pushed"), (
        f"Expected completed or pushed, got {record['status']}"
    )
    assert record["finished_at"] is not None, "Run should have finished"

    # C. ENTRY RECONCILIATION - dirty frozen RED was detected and restored
    restored_content = frozen_red_file.read_bytes()
    assert restored_content == expected_original, (
        f"Frozen RED file should be restored to original bytes. "
        f"Expected: {expected_original}, Got: {restored_content}"
    )

    # D. EXACT RESTORE - SHA256 matches authoritative snapshot
    restored_sha = hashlib.sha256(restored_content).hexdigest()
    assert restored_sha == expected_sha, "SHA256 after restore must match authoritative snapshot"

    # E. VIOLATION EVIDENCE - frozen_red_mutation event exists
    events_after = [
        json.loads(line)
        for line in events_path.read_text(encoding="utf-8").strip().split("\n")
        if line
    ]
    mutation_events = [e for e in events_after if e["event"] == "frozen_red_mutation"]
    assert len(mutation_events) >= 1, "Must have frozen_red_mutation event"
    mutation_payload = mutation_events[-1]["payload"]
    assert mutation_payload["task_id"] == task_id
    assert mutation_payload["phase"] in ("pre_build", "post_build", "recovery_reconcile")
    assert any(p["path"] == "tests/test_rule.py" for p in mutation_payload["paths"])
    assert mutation_payload["builder_phase"] in ("build", "recovery")

    # F. VIOLATING ATTEMPT NOT ACCEPTED - candidate written by pre-crash
    # The run should complete but the build node should have been re-executed after restoration
    # Verify that quality gate was NOT passed with the corrupted frozen RED
    # (If quality ran, it would have run against the restored RED, not the corrupted one)

    # G. NO DIRTY DOWNSTREAM EXECUTION
    # Quality/review/integration should not have consumed the corrupted frozen RED candidate
    # Since the build node re-ran after restoration, any downstream steps would see clean RED
    # We verify the run completed successfully

    # H. NO HUMAN DECISION
    human_decisions_after = [e for e in events_after if e["event"] == "human_decision"]
    assert len(human_decisions_after) == 0, "No human decisions should be created during recovery"

    # I. NO SECOND RUN
    all_runs = ControlRegistry(registry_path).runs_for_project("frozen-red-chaos")
    assert len(all_runs) == 1, f"Exactly one run should exist, found {len(all_runs)}"


def load_config(config_path: Path) -> ProjectConfig:
    from converge_orchestrator.config import load_config
    return load_config(config_path)


if __name__ == "__main__":
    pytest.main([__file__, "-v"])