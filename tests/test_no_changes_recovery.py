"""V33 no_changes run-termination regression tests.

V33 (run ``a5e0562d49c345b69c8f2fa95a191491``) proved a production workflow defect: a
legitimate zero-diff task for a selected non-PASS requirement (the planner's honest
verify-only plan for an already-satisfied requirement) terminated the ENTIRE
multi-requirement run (``route_after_integrate`` -> end) while six mandatory requirements,
including the predeclared ACCEPT-003 risk migration, remained unprocessed.

These tests prove the corrected semantics:

- a zero-diff candidate routes through the EXISTING bounded replan mechanism while budget
  remains, with durable precise Planner feedback;
- compliance semantics are untouched (local gates alone never fabricate PASS);
- budget exhaustion ends the run fail-closed with no empty PR, no fabricated commit, no
  ordinary HITL and no infinite loop;
- real candidates and spec drift keep exactly their previous routing.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from converge_orchestrator.git import diff
from converge_orchestrator.graph_service import build_graph
from converge_orchestrator.models import CIResult, ProjectConfig, PullRequestInfo
from converge_orchestrator.spec import compile_contract, sha256_file
from converge_orchestrator.workflow import integrate, route_after_integrate


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


# ---------------------------------------------------------------------------
# Pure router semantics
# ---------------------------------------------------------------------------


def _routing_config(tmp_path: Path, *, github: bool) -> ProjectConfig:
    return ProjectConfig(
        repo_path=tmp_path,
        requirements_path=tmp_path / "architecture.md",
        state_dir=tmp_path / ".converge",
        worktree_dir=tmp_path / ".converge" / "worktrees",
        agents={},
        github_repo="example/convergence-target" if github else None,
        require_spec_read_only=False,
    )


def test_zero_diff_with_replan_budget_routes_to_bounded_replan(tmp_path: Path) -> None:
    config = _routing_config(tmp_path, github=True)
    state = {"config_path": "converge.yaml", "commit_sha": None, "replan_attempts": 1}
    with patch("converge_orchestrator.workflow.load_config", return_value=config):
        assert route_after_integrate(state) == "replan"  # type: ignore[arg-type]


def test_zero_diff_with_exhausted_budget_fails_closed(tmp_path: Path) -> None:
    config = _routing_config(tmp_path, github=True)
    state = {"config_path": "converge.yaml", "commit_sha": None, "replan_attempts": 2}
    with patch("converge_orchestrator.workflow.load_config", return_value=config):
        assert route_after_integrate(state) == "end"  # type: ignore[arg-type]


def test_real_commit_keeps_pr_routing(tmp_path: Path) -> None:
    config = _routing_config(tmp_path, github=True)
    state = {"config_path": "converge.yaml", "commit_sha": "abc", "replan_attempts": 0}
    with patch("converge_orchestrator.workflow.load_config", return_value=config):
        assert route_after_integrate(state) == "pr"  # type: ignore[arg-type]
        without_github = _routing_config(tmp_path, github=False)
        with patch(
            "converge_orchestrator.workflow.load_config", return_value=without_github
        ):
            assert route_after_integrate(state) == "end"  # type: ignore[arg-type]


def test_spec_change_remains_terminal_even_with_commit(tmp_path: Path) -> None:
    config = _routing_config(tmp_path, github=True)
    state = {
        "config_path": "converge.yaml",
        "status": "spec_changed",
        "commit_sha": "abc",
        "replan_attempts": 0,
    }
    with patch("converge_orchestrator.workflow.load_config", return_value=config):
        assert route_after_integrate(state) == "end"  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# integrate(): zero-diff feedback + compliance semantics
# ---------------------------------------------------------------------------


def _unit_worktree(tmp_path: Path) -> Path:
    worktree = tmp_path / "wt"
    worktree.mkdir()
    _git(worktree, "init", "-b", "main")
    _git(worktree, "config", "user.email", "unit@example.invalid")
    _git(worktree, "config", "user.name", "Unit")
    (worktree / "README.md").write_text("baseline\n", encoding="utf-8")
    _git(worktree, "add", "-A")
    _git(worktree, "commit", "-m", "base")
    _git(worktree, "update-ref", "refs/remotes/origin/main", "HEAD")
    return worktree


def _unit_config(tmp_path: Path) -> ProjectConfig:
    requirements = tmp_path / "architecture.md"
    requirements.write_text(
        "# Goal\nARCH-001 The README must document the release command.\n",
        encoding="utf-8",
    )
    return ProjectConfig(
        repo_path=tmp_path / "base",
        requirements_path=requirements,
        state_dir=tmp_path / ".converge",
        worktree_dir=tmp_path / ".converge" / "worktrees",
        agents={},
        require_spec_read_only=False,
    )


def _unit_state(
    tmp_path: Path, worktree: Path, config: ProjectConfig
) -> tuple[dict, SimpleNamespace]:
    store = SimpleNamespace(write_json=Mock(), append_event=Mock())
    state = {
        "config_path": str(tmp_path / "converge.yaml"),
        "run_id": "run-1",
        "requirements_hash": sha256_file(config.requirements_path),
        "task": {
            "id": "ARCH-001-0038",
            "requirement_ids": ["ARCH-001"],
            "title": "Verify README release command",
            "objective": "Confirm README documents the release command; no code change needed",
            "allowed_paths": ["README.md"],
            "acceptance": ["README documents the release command"],
            "max_diff_lines": 10,
            "risk": "low",
            "risk_flags": [],
            "change_kind": "docs",
            "tdd": {"mode": "not_applicable"},
        },
        "worktree": str(worktree),
        "branch": "converge/arch-001-0038",
        "compliance": {
            "entries": {
                "ARCH-001": {
                    "requirement_id": "ARCH-001",
                    "status": "unverified",
                    "evidence": [],
                }
            },
            "mandatory_regressions": 0,
        },
        "quality_results": [
            {
                "name": "target-test-suite",
                "ok": True,
                "required": True,
                "returncode": 0,
                "output": "ok",
            }
        ],
        "replan_attempts": 0,
    }
    return state, store


def test_integrate_zero_diff_records_feedback_without_fabricating_pass(
    tmp_path: Path,
) -> None:
    config = _unit_config(tmp_path)
    worktree = _unit_worktree(tmp_path)
    state, store = _unit_state(tmp_path, worktree, config)

    with (
        patch("converge_orchestrator.workflow.load_config", return_value=config),
        patch("converge_orchestrator.workflow._evidence", return_value=store),
        patch("converge_orchestrator.workflow._write_compliance"),
    ):
        result = integrate(state)  # type: ignore[arg-type]

    assert result["status"] == "no_changes"
    assert result["commit_sha"] is None
    entry = result["compliance"]["entries"]["ARCH-001"]
    assert entry["status"] == "partial"
    assert entry["status"] != "pass"
    control = result["baseline"]["planner_control"]
    assert control["last_failure_kind"] == "no_changes"
    assert control["target_requirement_id"] == "ARCH-001"
    assert "no mergeable diff" in control["last_error"]
    assert "verify-only/no-op work cannot satisfy" in control["last_error"]
    assert "forbidden" in control["last_error"]
    pushed_event = store.append_event.call_args_list[0]
    assert pushed_event.args[1] == "pushed"
    assert pushed_event.args[2]["commit_sha"] is None


def test_integrate_zero_diff_never_promotes_partial_or_unverified_to_pass(
    tmp_path: Path,
) -> None:
    config = _unit_config(tmp_path)
    worktree = _unit_worktree(tmp_path)
    state, store = _unit_state(tmp_path, worktree, config)
    state["compliance"]["entries"]["ARCH-001"]["status"] = "partial"

    with (
        patch("converge_orchestrator.workflow.load_config", return_value=config),
        patch("converge_orchestrator.workflow._evidence", return_value=store),
        patch("converge_orchestrator.workflow._write_compliance"),
    ):
        result = integrate(state)  # type: ignore[arg-type]

    assert result["compliance"]["entries"]["ARCH-001"]["status"] == "partial"


# ---------------------------------------------------------------------------
# End-to-end: recoverable zero diff then real change (CASE 2)
# ---------------------------------------------------------------------------


def _e2e_repository(tmp_path: Path) -> tuple[Path, Path]:
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


def _e2e_config(tmp_path: Path, repo: Path, requirements: Path) -> Path:
    config = {
        "version": 1,
        "project": {
            "repo_path": str(repo),
            "requirements_path": str(requirements),
            "state_dir": str(tmp_path / "state"),
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
                    "name": "release-marker",
                    "command": [sys.executable, "-c", "print('release gate ok')"],
                    "required": True,
                    "timeout_seconds": 30,
                }
            ],
        },
        "workflow": {
            "max_repair_attempts": 1,
            "max_replans": 2,
            "max_iterations": 8,
        },
    }
    config_path = tmp_path / "converge.yaml"
    config_path.write_text(json.dumps(config, indent=2) + "\n", encoding="utf-8")
    return config_path


def _planner_task(requirement_id: str, *, real: bool) -> dict:
    if real:
        return {
            "id": "E2E-001",
            "requirement_ids": [requirement_id],
            "title": "Document the deterministic release command",
            "objective": "Add the Release command section to README.md.",
            "allowed_paths": ["README.md"],
            "acceptance": ["README documents the release command"],
            "max_diff_lines": 10,
            "risk": "low",
            "risk_flags": [],
            "change_kind": "docs",
            "tdd": {"mode": "not_applicable"},
        }
    return {
        "id": "E2E-001",
        "requirement_ids": [requirement_id],
        "title": "Verify README release command",
        "objective": (
            "Confirm README documents the release command; no code change needed"
        ),
        "allowed_paths": ["README.md"],
        "acceptance": ["README documents the release command"],
        "max_diff_lines": 10,
        "risk": "low",
        "risk_flags": [],
        "change_kind": "docs",
        "tdd": {"mode": "not_applicable"},
    }


def test_zero_diff_recovers_and_real_plan_merges_without_hitl(tmp_path: Path) -> None:
    repo, _origin = _e2e_repository(tmp_path)
    requirements = tmp_path / "architecture.md"
    requirements.write_text(
        "# Goal\n"
        "ARCH-001 The repository README must document the deterministic release command.\n",
        encoding="utf-8",
    )
    requirement_id = compile_contract(requirements).requirements[0].id
    config_path = _e2e_config(tmp_path, repo, requirements)
    agent_calls: list[str] = []
    planner_prompts: list = []

    def fake_invoke(_adapter, role, prompt, cwd):
        agent_calls.append(role)
        if role == "planner":
            planner_prompts.append(prompt)
            if len(planner_prompts) == 1:
                return SimpleNamespace(
                    ok=True,
                    output=json.dumps(_planner_task(requirement_id, real=False)),
                    context={"budget_status": "ok"},
                )
            return SimpleNamespace(
                ok=True,
                output=json.dumps(_planner_task(requirement_id, real=True)),
                context={"budget_status": "ok"},
            )
        if role == "builder":
            if len(agent_calls) == 2:
                return SimpleNamespace(
                    ok=True, output="verified, no change needed", context=None
                )
            Path(cwd, "README.md").write_text(
                "baseline\n\n## Release command\n\ndeterministic-release\n",
                encoding="utf-8",
            )
            return SimpleNamespace(ok=True, output="candidate written", context=None)
        if role == "reviewer":
            return SimpleNamespace(
                ok=True,
                output=json.dumps({"verdict": "pass", "findings": [], "confidence": 1.0}),
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
    assert result.get("human_decisions") in (None, [])
    assert agent_calls.count("planner") == 2
    assert agent_calls.count("builder") == 2
    feedback = next(
        section
        for section in planner_prompts[1].advisory
        if section.name == "planner validation feedback"
    )
    assert "no mergeable diff" in feedback.text
    assert "verify-only/no-op work cannot satisfy" in feedback.text
    marker = "## Release command" in _git(repo, "show", "HEAD:README.md")
    assert marker


# ---------------------------------------------------------------------------
# End-to-end: repeated zero diff exhausts budget fail-closed (CASE 3)
# ---------------------------------------------------------------------------


def test_repeated_zero_diff_exhausts_budget_fail_closed_without_hitl(
    tmp_path: Path,
) -> None:
    repo, _origin = _e2e_repository(tmp_path)
    requirements = tmp_path / "architecture.md"
    requirements.write_text(
        "# Goal\n"
        "ARCH-001 The repository README must document the deterministic release command.\n",
        encoding="utf-8",
    )
    requirement_id = compile_contract(requirements).requirements[0].id
    config_path = _e2e_config(tmp_path, repo, requirements)
    agent_calls: list[str] = []
    pr_calls: list[int] = []

    def fake_invoke(_adapter, role, prompt, cwd):
        agent_calls.append(role)
        if role == "planner":
            return SimpleNamespace(
                ok=True,
                output=json.dumps(_planner_task(requirement_id, real=False)),
                context={"budget_status": "ok"},
            )
        if role == "builder":
            return SimpleNamespace(
                ok=True, output="verified, no change needed", context=None
            )
        if role == "reviewer":
            return SimpleNamespace(
                ok=True,
                output=json.dumps({"verdict": "pass", "findings": [], "confidence": 1.0}),
                context=None,
            )
        raise AssertionError(f"unexpected agent role: {role}")

    class FakeGitHubAdapter:
        def __init__(self, config):
            self.config = config

        def ensure_pull_request(self, *, head, base, title, body):
            pr_calls.append(1)
            return PullRequestInfo(
                number=17,
                url="https://github.invalid/example/convergence-target/pull/17",
                head_sha=head,
                state="open",
            )

        def ci_status(self, head_sha):
            return CIResult(status="pass", head_sha=head_sha, checks=[])

        def merge(self, number):
            raise AssertionError("merge must never be reached for repeated zero-diff")

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

    assert result["status"] == "no_changes"
    assert result["commit_sha"] is None
    assert result.get("replan_attempts") == 2
    assert result.get("human_decisions") in (None, [])
    assert agent_calls.count("planner") == 3
    assert agent_calls.count("builder") == 3
    assert pr_calls == []
    assert "## Release command" not in _git(repo, "show", "HEAD:README.md")


# ---------------------------------------------------------------------------
# Adversarial: budget accounting
# ---------------------------------------------------------------------------


def test_replan_counter_is_not_reset_by_zero_diff_recovery(tmp_path: Path) -> None:
    from converge_orchestrator.workflow import replan as replan_node

    store = SimpleNamespace(write_json=Mock(), append_event=Mock())
    state = {
        "config_path": "converge.yaml",
        "run_id": "run-1",
        "task": {"id": "ARCH-001-0038"},
        "status": "no_changes",
        "replan_attempts": 1,
        "tdd_replan_attempts": 0,
    }
    with patch("converge_orchestrator.workflow._evidence", return_value=store), patch(
        "converge_orchestrator.workflow._discard_current_workspace"
    ):
        result = replan_node(state)  # type: ignore[arg-type]
    assert result["replan_attempts"] == 2
    assert result["tdd_replan_attempts"] == 0
    event = store.append_event.call_args_list[0]
    assert event.args[2]["failure_class"] == "other"


def test_candidate_diff_still_reports_real_changes(tmp_path: Path) -> None:
    worktree = _unit_worktree(tmp_path)
    (worktree / "README.md").write_text(
        "baseline\n\n## Release command\n", encoding="utf-8"
    )
    config = _unit_config(tmp_path)
    patch_text = diff(worktree, config.base_branch)
    assert "Release command" in patch_text