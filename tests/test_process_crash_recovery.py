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
    _git(repo, "add", "README.md")
    _git(repo, "commit", "-m", "baseline")
    _git(repo, "push", "-u", "origin", "main")
    return repo


def _config(tmp_path: Path, repo: Path) -> tuple[Path, Path, Path]:
    state_dir = tmp_path / "state"
    worktree_dir = tmp_path / "worktrees"
    requirements = tmp_path / "architecture.md"
    requirements.write_text("The candidate must survive process death.\n", encoding="utf-8")
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
                "agents: {}",
                "",
            ]
        ),
        encoding="utf-8",
    )
    return config_path, state_dir, worktree_dir


def _worker_environment() -> dict[str, str]:
    environment = dict(os.environ)
    source_root = str(Path(__file__).resolve().parents[1] / "src")
    current = environment.get("PYTHONPATH")
    environment["PYTHONPATH"] = (
        source_root if not current else os.pathsep.join((source_root, current))
    )
    return environment


@pytest.mark.parametrize("_race_attempt", range(3))
def test_service_process_recovers_uncheckpointed_worktree_without_duplication(
    tmp_path: Path,
    _race_attempt: int,
) -> None:
    del _race_attempt
    repo = _repository(tmp_path)
    config_path, state_dir, worktree_dir = _config(tmp_path, repo)
    registry_path = state_dir / "control.sqlite"
    worker = Path(__file__).parent / "fixtures" / "process_crash_worker.py"
    environment = _worker_environment()

    crashed = subprocess.run(
        [sys.executable, str(worker), "crash", str(registry_path), str(config_path)],
        cwd=Path(__file__).resolve().parents[1],
        env=environment,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        timeout=20,
    )
    assert crashed.returncode == 91, crashed.stdout

    crashed_record = ControlRegistry(registry_path).runs_for_project("chaos")[0]
    assert crashed_record["status"] == "running"
    assert crashed_record["finished_at"] is None
    assert crashed_record["lease_owner"]

    # The first controller died without releasing its lease. Restart only after its deliberately
    # shortened test TTL expires, mirroring production failover after the normal lease window.
    time.sleep(1.2)
    recovered = subprocess.run(
        [sys.executable, str(worker), "recover", str(registry_path)],
        cwd=Path(__file__).resolve().parents[1],
        env=environment,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        timeout=20,
    )
    assert recovered.returncode == 0, recovered.stdout

    record = ControlRegistry(registry_path).runs_for_project("chaos")[0]
    assert record["status"] == "completed"
    assert record["finished_at"] is not None
    assert record["lease_owner"] is None
    assert (state_dir / "chaos-attempts.txt").read_text(encoding="utf-8") == "2"

    candidate = worktree_dir / "chaos-001"
    assert (candidate / "uncheckpointed-change.txt").read_text(encoding="utf-8") == (
        "preserve across process death\n"
    )
    assert _git(candidate, "status", "--porcelain") == "?? uncheckpointed-change.txt"

    worktree_entries = _git(repo, "worktree", "list", "--porcelain")
    registered_paths = [
        Path(line.removeprefix("worktree ")).resolve()
        for line in worktree_entries.splitlines()
        if line.startswith("worktree ")
    ]
    assert registered_paths.count(candidate.resolve()) == 1
    branches = _git(repo, "branch", "--list", "converge/chaos-001").splitlines()
    assert len(branches) == 1
