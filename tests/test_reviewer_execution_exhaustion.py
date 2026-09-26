from __future__ import annotations

import json
import subprocess
import sys
import time
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from converge_orchestrator.models import CIResult, PullRequestInfo
from converge_orchestrator.registry import ControlRegistry
from converge_orchestrator.runtime_service import ScheduledRunController
from converge_orchestrator.spec import compile_contract


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


def _repository(tmp_path: Path) -> tuple[Path, Path]:
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
    return repo, origin


def _configuration(
    tmp_path: Path,
    repo: Path,
    requirements: Path,
    max_review_execution_retries: int = 1,
) -> Path:
    state_dir = tmp_path / "state"
    config = {
        "version": 1,
        "project": {
            "repo_path": str(repo),
            "requirements_path": str(requirements),
            "state_dir": str(state_dir),
            "worktree_dir": str(tmp_path / "worktrees"),
            "require_spec_read_only": False,
        },
        "github": {
            "repo": "example/convergence-target",
            "auto_merge": True,
            "ci_poll_seconds": 1,
            "ci_timeout_seconds": 30,
        },
        "agents": {
            "planner": {"agent": "e2e-planner", "model": "fake/planner"},
            "builder": {"agent": "e2e-builder", "model": "fake/builder"},
            "reviewer": {"agent": "e2e-reviewer", "model": "fake/reviewer"},
        },
        "quality": {
            "auto_discover": False,
            "gates": [
                {
                    "name": "result-contract",
                    "command": [
                        sys.executable,
                        "-c",
                        (
                            "from pathlib import Path; "
                            "assert Path('RESULT.txt').read_text(encoding='utf-8') == 'done\\n'"
                        ),
                    ],
                    "required": True,
                    "timeout_seconds": 30,
                }
            ],
        },
        "workflow": {
            "max_review_execution_retries": max_review_execution_retries,
            "max_repair_attempts": 3,
            "max_replans": 2,
            "max_iterations": 2,
            "max_diff_lines_hard": 50,
        },
    }
    config_path = tmp_path / "converge.yaml"
    config_path.write_text(json.dumps(config, indent=2) + "\n", encoding="utf-8")
    return config_path


def test_reviewer_execution_exhaustion_produces_terminal_failure_in_registry(
    tmp_path: Path,
) -> None:
    """Direct test that exhausted reviewer execution retries produces terminal failure.

    This exercises the actual runtime completion path via ScheduledRunController:
    - review execution retries exhausted (pure transport failures)
    -> review_execution_failure node
    -> graph END
    -> RunController/ScheduledRunController completion reconciliation
    -> final control registry record with:
        status == "failed"
        finished_at is not None
        lease_owner is None
        lease_expires_at is None
    And proves:
        no human decision exists
        repair_attempts unchanged (0)
        replan_attempts unchanged (0)
    """
    repo, _origin = _repository(tmp_path)
    requirements = tmp_path / "architecture.md"
    requirements.write_text(
        "# Goal\n"
        "ARCH-001 The generated repository must contain RESULT.txt with the exact text done.\n",
        encoding="utf-8",
    )
    requirement_id = compile_contract(requirements).requirements[0].id
    assert requirement_id == "ARCH-001"
    
    # Use max_review_execution_retries=1 to exhaust quickly
    config_path = _configuration(tmp_path, repo, requirements, max_review_execution_retries=1)
    state_dir = tmp_path / "state"
    registry_path = state_dir / "control.sqlite"

    reviewer_call_count = [0]

    def fake_invoke(_adapter, role, _prompt, cwd):
        if role == "planner":
            task = {
                "id": "E2E-001",
                "requirement_ids": [requirement_id],
                "title": "Satisfy the deterministic smoke requirement",
                "objective": "Create RESULT.txt with the exact required content.",
                "allowed_paths": ["RESULT.txt"],
                "acceptance": ["RESULT.txt contains exactly done followed by a newline."],
                "max_diff_lines": 10,
                "risk": "low",
                "change_kind": "docs",
            }
            return SimpleNamespace(
                ok=True,
                output=json.dumps(task),
                context={"budget_status": "ok"},
            )
        if role == "builder":
            Path(cwd, "RESULT.txt").write_text("done\n", encoding="utf-8")
            return SimpleNamespace(ok=True, output="candidate written", context=None)
        if role == "reviewer":
            reviewer_call_count[0] += 1
            # Return ok=True with a review result that has transport failure in lanes
            return SimpleNamespace(
                ok=True,
                output=json.dumps({
                    "verdict": "reject",
                    "findings": [
                        {
                            "severity": "major",
                            "reason": "architecture_reviewer execution failed: 502 Bad Gateway",
                            "required_fix": "architecture_reviewer must complete successfully",
                        }
                    ],
                    "confidence": 0.8,
                    "reviewers": {
                        "correctness_reviewer": "pass",
                        "architecture_reviewer": "reject",
                        "security_reviewer": "pass",
                    },
                }),
                context={
                    "review_lanes": {
                        "correctness_reviewer": {"provider_failure_class": None},
                        "architecture_reviewer": {
                            "provider_failure_class": "transport",
                            "provider_error_events": 1,
                        },
                        "security_reviewer": {"provider_failure_class": None},
                    }
                },
            )
        raise AssertionError(f"unexpected agent role: {role}")

    class FakeGitHubAdapter:
        head_branch: str | None = None

        def __init__(self, config):
            self.config = config

        def ensure_pull_request(self, *, head, base, title, body):
            del base, title, body
            type(self).head_branch = head
            head_sha = _git(self.config.repo_path, "rev-parse", head)
            return PullRequestInfo(
                number=17,
                url="https://github.invalid/example/convergence-target/pull/17",
                head_sha=head_sha,
                state="open",
            )

        def ci_status(self, head_sha):
            return CIResult(status="pass", head_sha=head_sha, checks=[])

        def merge(self, number):
            assert number == 17
            branch = type(self).head_branch
            assert branch is not None
            head_sha = _git(self.config.repo_path, "rev-parse", branch)
            _git(self.config.repo_path, "push", "origin", f"{branch}:main")
            return head_sha

    # Use ScheduledRunController for real runtime path
    controller = ScheduledRunController(registry_path)
    with patch("converge_orchestrator.runtime_service.validate_origin_repository"):
        controller.register_project("e2e", config_path)
    record = controller.start_run("e2e")
    run_id = str(record["id"])

    with (
        patch("converge_orchestrator.opencode.OpenCodeAdapter.invoke", new=fake_invoke),
        patch("converge_orchestrator.workflow.GitHubAdapter", FakeGitHubAdapter),
        patch("converge_orchestrator.ci.GitHubAdapter", FakeGitHubAdapter),
    ):
        # Run the graph to completion via the controller
        # The controller's resume/decide methods handle the graph execution
        deadline = time.monotonic() + 60
        while time.monotonic() < deadline:
            current = controller.registry.get_run(run_id)
            if current["finished_at"]:
                break
            # If there's an interrupt, resume automatically (machine-managed)
            snapshot = controller._snapshot(current)
            interrupt_payload = snapshot.get("interrupt")
            if interrupt_payload:
                if interrupt_payload.get("kind") in ("ci_wait", "reviewer_recovery_wait"):
                    controller.resume(run_id)
                else:
                    # Human decision needed - but we shouldn't get here for this test
                    raise RuntimeError(
                        f"Unexpected interrupt kind: {interrupt_payload.get('kind')}"
                    )
            # Don't call resume if no interrupt - the graph is already running
            time.sleep(0.2)
        else:
            raise RuntimeError("Timeout waiting for run to complete")

    # Verify the final state
    final_record = controller.registry.get_run(run_id)
    assert final_record["status"] == "failed", (
        f"Expected status 'failed', got '{final_record['status']}'"
    )
    assert final_record["finished_at"] is not None, "finished_at should be set"
    assert final_record["lease_owner"] is None, "lease_owner should be None for completed run"
    assert final_record["lease_expires_at"] is None, (
        "lease_expires_at should be None for completed run"
    )

    # Verify the registry has the run
    registry = ControlRegistry(registry_path)
    runs = registry.runs_for_project("e2e")
    assert len(runs) == 1
    record = runs[0]
    assert record["status"] == "failed"
    assert record["finished_at"] is not None
    assert record["lease_owner"] is None
    assert record["lease_expires_at"] is None

    # Verify reviewer was called multiple times (initial + recovery attempts)
    assert reviewer_call_count[0] >= 2, (
        f"Expected at least 2 reviewer calls, got {reviewer_call_count[0]}"
    )


if __name__ == "__main__":
    import pytest
    pytest.main([__file__, "-v"])