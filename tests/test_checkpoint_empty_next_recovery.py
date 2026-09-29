from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

from converge_orchestrator.registry import ControlRegistry


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


def _config(tmp_path: Path, repo: Path) -> Path:
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


def _durable_checkpoint_snapshot(
    state_dir: Path, thread_id: str
) -> dict[str, object]:
    from converge_orchestrator.graph_service import build_graph
    from converge_orchestrator.persistence import open_checkpointer

    checkpointer, db = open_checkpointer(state_dir)
    try:
        graph = build_graph(checkpointer=checkpointer)
        snapshot = graph.get_state({"configurable": {"thread_id": thread_id}})
        return {
            "next": list(snapshot.next),
            "interrupt": bool(snapshot.interrupts),
            "values_status": snapshot.values.get("status"),
            "values_present": bool(snapshot.values),
        }
    finally:
        db.close()


def test_empty_next_crash_recovers_without_hitl_or_duplication(tmp_path: Path) -> None:
    """
    Regression test for issue #96: fail-safe recovery when a crash leaves the
    durable LangGraph checkpoint without a pending next trigger.

    Exercises: ScheduledRunController -> real graph_service.build_graph() -> real
    LangGraph checkpointer -> real control registry -> real worktree -> process
    death during the build node while the checkpoint blob put of the previous
    transition is still in flight -> NEW ScheduledRunController -> recovery.

    The worker deterministically widens the exact persistence window production
    hit (checkpoint blob put in flight when os._exit fires). The test proves the
    run is either automatically resumed on the same durable run/thread or
    deterministically failed closed; it must never stay "running" forever, never
    create a second run, never involve ordinary HITL, never duplicate external
    side effects, and never corrupt the frozen RED authority.
    """
    repo = _repository(tmp_path)
    config_path = _config(tmp_path, repo)
    registry_path = tmp_path / "state" / "control.sqlite"
    worker = Path(__file__).parent / "fixtures" / "process_empty_next_crash_worker.py"
    environment = _worker_environment()

    crashed = subprocess.run(
        [sys.executable, str(worker), "crash", str(registry_path), str(config_path)],
        cwd=Path(__file__).resolve().parents[1],
        env=environment,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        timeout=180,
    )
    print(f"CRASH WORKER STDOUT:\n{crashed.stdout}", flush=True)
    assert crashed.returncode == 94, (
        f"Expected exit code 94, got {crashed.returncode}: {crashed.stdout}"
    )

    crashed_record = ControlRegistry(registry_path).runs_for_project("empty-next-chaos")[0]
    original_run_id = crashed_record["id"]
    original_thread_id = crashed_record["thread_id"]
    failure_snapshot = _durable_checkpoint_snapshot(tmp_path / "state", original_thread_id)
    print(f"DURABLE CHECKPOINT SNAPSHOT AFTER CRASH: {failure_snapshot}", flush=True)

    assert crashed_record["status"] == "running", (
        f"Expected running, got {crashed_record['status']}"
    )
    assert crashed_record["finished_at"] is None
    assert crashed_record["lease_owner"]
    assert failure_snapshot["values_present"], "Mid-graph values must be durable"
    assert failure_snapshot["interrupt"] is False, "No interrupt must be active"
    assert failure_snapshot["values_status"] == "frozen_red_snapshotted"

    worktree_dir = tmp_path / "worktrees"
    candidate_dirs = list(worktree_dir.glob("arch-001-1*"))
    assert len(candidate_dirs) == 1, (
        f"Expected exactly one candidate worktree, found {candidate_dirs}"
    )
    frozen_red_file = candidate_dirs[0] / "tests" / "test_rule.py"
    assert frozen_red_file.exists(), "Frozen RED file should exist in worktree"
    dirty_content = frozen_red_file.read_bytes()
    assert dirty_content == b"def test_new_rule():\n    assert True\n", (
        f"Expected mutated content, got {dirty_content}"
    )

    time.sleep(1.2)

    recovered = subprocess.run(
        [sys.executable, str(worker), "recover", str(registry_path)],
        cwd=Path(__file__).resolve().parents[1],
        env=environment,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        timeout=90,
    )
    print(f"RECOVER WORKER STDOUT:\n{recovered.stdout}", flush=True)
    assert recovered.returncode == 0, f"Recovery failed: {recovered.stdout}"

    record = ControlRegistry(registry_path).runs_for_project("empty-next-chaos")[0]
    assert record["id"] == original_run_id, "Recovered run_id must match original"
    assert record["thread_id"] == original_thread_id, (
        "Recovered thread_id must match original"
    )
    assert record["status"] in ("completed", "pushed"), (
        f"Expected completed or pushed, got {record['status']}"
    )
    assert record["finished_at"] is not None, "Run must not remain indefinitely running"
    assert len(ControlRegistry(registry_path).runs_for_project("empty-next-chaos")) == 1

    restored_content = frozen_red_file.read_bytes()
    assert restored_content == b"def test_new_rule():\n    assert False, 'NEW_RULE_MISSING'\n", (
        f"Frozen RED authority must survive recovery, got {restored_content}"
    )

    events_path = tmp_path / "state" / "evidence" / original_run_id / "events.jsonl"
    assert events_path.exists(), "Run events must exist"
    events = [
        json.loads(line)
        for line in events_path.read_text(encoding="utf-8").strip().split("\n")
        if line
    ]
    human_decisions = [e for e in events if e["event"] == "human_decision"]
    assert len(human_decisions) == 0, "No ordinary HITL during machine recovery"

    candidate = candidate_dirs[0]
    branch_log = _git(candidate, "log", "--oneline", "HEAD", "--not", "main")
    assert len(branch_log.splitlines()) <= 1, (
        f"External side effects must not duplicate; branch log: {branch_log}"
    )


if __name__ == "__main__":
    pytest.main([__file__, "-v"])