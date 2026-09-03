from __future__ import annotations

import json
import os
import subprocess
import sys
from datetime import datetime
from pathlib import Path

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
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(
        ["git", "init", "--initial-branch=main"],
        cwd=repo,
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
    return repo


def _config(tmp_path: Path, repo: Path) -> tuple[Path, Path]:
    state_dir = tmp_path / "state"
    requirements = tmp_path / "architecture.md"
    requirements.write_text("CI waits must survive service restarts.\n", encoding="utf-8")
    config_path = tmp_path / "converge.yaml"
    config_path.write_text(
        "\n".join(
            [
                "version: 1",
                "project:",
                f"  repo_path: {json.dumps(str(repo))}",
                f"  requirements_path: {json.dumps(str(requirements))}",
                f"  state_dir: {json.dumps(str(state_dir))}",
                "  require_spec_read_only: false",
                "github:",
                "  ci_poll_seconds: 3",
                "  ci_timeout_seconds: 30",
                "agents: {}",
                "",
            ]
        ),
        encoding="utf-8",
    )
    return config_path, state_dir


def _worker_environment() -> dict[str, str]:
    environment = dict(os.environ)
    source_root = str(Path(__file__).resolve().parents[1] / "src")
    current = environment.get("PYTHONPATH")
    environment["PYTHONPATH"] = (
        source_root if not current else os.pathsep.join((source_root, current))
    )
    return environment


def test_service_restart_restores_machine_ci_wait_without_human_resume(
    tmp_path: Path,
) -> None:
    repo = _repository(tmp_path)
    config_path, state_dir = _config(tmp_path, repo)
    registry_path = state_dir / "control.sqlite"
    worker = Path(__file__).parent / "fixtures" / "process_ci_wait_worker.py"
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
    assert crashed.returncode == 94, crashed.stdout

    registry = ControlRegistry(registry_path)
    before = registry.runs_for_project("chaos-ci")
    assert len(before) == 1
    crashed_record = before[0]
    assert crashed_record["status"] == "waiting_ci"
    assert crashed_record["finished_at"] is None
    assert crashed_record["lease_owner"] is None

    interrupt_snapshot = json.loads(
        (state_dir / "ci-wait-interrupt.json").read_text(encoding="utf-8")
    )
    interrupt_payload = interrupt_snapshot["interrupt"]
    assert interrupt_snapshot["run_id"] == crashed_record["id"]
    assert interrupt_snapshot["thread_id"] == crashed_record["thread_id"]
    assert interrupt_payload["kind"] == "ci_wait"
    assert interrupt_payload["run_id"] == crashed_record["id"]
    assert interrupt_payload["head_sha"] == "candidate-sha"
    wake_at = datetime.fromisoformat(interrupt_payload["wake_at"])
    assert wake_at.tzinfo is not None
    assert not (state_dir / "ci-wait-finished.json").exists()

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

    after = ControlRegistry(registry_path).runs_for_project("chaos-ci")
    assert len(after) == 1
    record = after[0]
    assert record["id"] == crashed_record["id"]
    assert record["thread_id"] == crashed_record["thread_id"]
    assert record["status"] == "completed"
    assert record["finished_at"] is not None
    assert record["lease_owner"] is None

    finished = json.loads((state_dir / "ci-wait-finished.json").read_text(encoding="utf-8"))
    assert finished["run_id"] == crashed_record["id"]
    assert finished["thread_id"] == crashed_record["thread_id"]
    assert datetime.fromisoformat(finished["finished_at"]).tzinfo is not None
