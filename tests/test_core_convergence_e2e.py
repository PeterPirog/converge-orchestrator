from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from converge_orchestrator.graph_service import build_graph
from converge_orchestrator.models import CIResult, PullRequestInfo
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
    _git(repo, "config", "user.name", "Converge E2E")
    (repo / "README.md").write_text("baseline\n", encoding="utf-8")
    _git(repo, "add", "README.md")
    _git(repo, "commit", "-m", "baseline")
    _git(repo, "push", "-u", "origin", "main")
    return repo, origin


def _configuration(tmp_path: Path, repo: Path, requirements: Path) -> Path:
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
            "max_repair_attempts": 1,
            "max_replans": 1,
            "max_iterations": 2,
            "max_diff_lines_hard": 50,
        },
    }
    config_path = tmp_path / "converge.yaml"
    config_path.write_text(json.dumps(config, indent=2) + "\n", encoding="utf-8")
    return config_path


def test_canonical_graph_converges_one_requirement_without_hitl(tmp_path: Path) -> None:
    repo, _origin = _repository(tmp_path)
    requirements = tmp_path / "architecture.md"
    requirements.write_text(
        "# Goal\n"
        "ARCH-001 The generated repository must contain RESULT.txt with the exact text done.\n",
        encoding="utf-8",
    )
    requirement_id = compile_contract(requirements).requirements[0].id
    assert requirement_id == "ARCH-001"
    config_path = _configuration(tmp_path, repo, requirements)
    agent_calls: list[str] = []

    def fake_invoke(_adapter, role, _prompt, cwd):
        agent_calls.append(role)
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
            return SimpleNamespace(
                ok=True,
                output=json.dumps(
                    {
                        "verdict": "pass",
                        "findings": [],
                        "confidence": 1.0,
                    }
                ),
                context=None,
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

    graph = build_graph()
    initial = {
        "project_id": "e2e",
        "config_path": str(config_path),
        "run_id": "e2e-run",
        "thread_id": "e2e-thread",
    }
    with (
        patch("converge_orchestrator.opencode.OpenCodeAdapter.invoke", new=fake_invoke),
        patch("converge_orchestrator.workflow.GitHubAdapter", FakeGitHubAdapter),
        patch("converge_orchestrator.ci.GitHubAdapter", FakeGitHubAdapter),
    ):
        result = graph.invoke(initial)

    assert result["status"] == "converged"
    assert result["iteration"] == 1
    assert result["task"] is None
    assert agent_calls == ["planner", "builder", "reviewer"]
    assert (repo / "RESULT.txt").read_text(encoding="utf-8") == "done\n"
    assert _git(repo, "rev-parse", "HEAD") == _git(repo, "rev-parse", "origin/main")
    assert not (tmp_path / "worktrees" / "e2e-001").exists()
    assert _git(repo, "branch", "--list", "converge/e2e-001") == ""
    assert _git(repo, "ls-remote", "--heads", "origin", "converge/e2e-001") == ""

    compliance = json.loads((tmp_path / "state" / "compliance.json").read_text(encoding="utf-8"))
    assert compliance["entries"][requirement_id]["status"] == "pass"
