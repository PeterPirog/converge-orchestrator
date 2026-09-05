from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

from converge_orchestrator.models import ProjectConfig
from converge_orchestrator.sandbox import ExecutionSandbox, container_path_for

_IMAGE_ENV = "CONVERGE_TEST_SANDBOX_IMAGE"
_NETWORK_ENV = "CONVERGE_TEST_SANDBOX_NETWORK"

pytestmark = pytest.mark.skipif(
    not os.environ.get(_IMAGE_ENV),
    reason=f"{_IMAGE_ENV} is required for real container integration",
)


def test_digest_pinned_agent_container_enforces_read_only_workspace(tmp_path: Path) -> None:
    image = os.environ[_IMAGE_ENV]
    network = os.environ.get(_NETWORK_ENV, "converge-sandbox-ci")
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / ".git").mkdir()
    (repo / "marker.txt").write_text("immutable fixture\n", encoding="utf-8")
    requirements = tmp_path / "architecture.md"
    requirements.write_text("ARCH-001 Sandbox must isolate agent writes.\n", encoding="utf-8")

    cfg = ProjectConfig(
        repo_path=repo,
        requirements_path=requirements,
        require_spec_read_only=False,
        model_gateway={"kind": "existing"},
        sandbox={
            "mode": "container",
            "image": image,
            "agent_network": network,
            "quality_network": "none",
            "require_internal_agent_network": True,
            "read_only_root": True,
            "user": "host",
        },
        agents={},
    )
    sandbox = ExecutionSandbox(cfg)

    sandbox.preflight()
    result = sandbox.run(
        [
            "/bin/sh",
            "-lc",
            "test -r marker.txt && ! touch should-not-exist.txt",
        ],
        cwd=repo,
        scope="agent",
        writable_cwd=False,
    )

    assert result.returncode == 0, result.stdout
    assert not (repo / "should-not-exist.txt").exists()


def test_linked_worktree_git_inspection_is_read_only(tmp_path: Path) -> None:
    image = os.environ[_IMAGE_ENV]
    network = os.environ.get(_NETWORK_ENV, "converge-sandbox-ci")
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "README.md").write_text("readme\n", encoding="utf-8")
    worktree = tmp_path / "worktree"
    for args in (
        ["git", "init", "-q", str(repo)],
        ["git", "-C", str(repo), "config", "user.email", "converge@example.test"],
        ["git", "-C", str(repo), "config", "user.name", "Converge Test"],
        ["git", "-C", str(repo), "add", "-A"],
        ["git", "-C", str(repo), "commit", "-q", "-m", "init"],
        [
            "git",
            "-C",
            str(repo),
            "worktree",
            "add",
            "-q",
            str(worktree),
            "-b",
            "task-1",
        ],
    ):
        subprocess.run(args, check=True, capture_output=True)
    requirements = tmp_path / "architecture.md"
    requirements.write_text("ARCH-001 Sandbox must isolate git metadata.\n", encoding="utf-8")

    cfg = ProjectConfig(
        repo_path=repo,
        requirements_path=requirements,
        require_spec_read_only=False,
        model_gateway={"kind": "existing"},
        sandbox={
            "mode": "container",
            "image": image,
            "agent_network": network,
            "quality_network": "none",
            "require_internal_agent_network": True,
            "read_only_root": True,
            "user": "host",
        },
        agents={},
    )
    sandbox = ExecutionSandbox(cfg)
    sandbox.preflight()

    git_probe = sandbox.run(
        ["/bin/sh", "-lc", "command -v git >/dev/null 2>&1"],
        cwd=worktree,
        scope="quality",
        writable_cwd=True,
    )
    if git_probe.returncode != 0:
        pytest.skip("sandbox image has no git; run with a git-capable image for this proof")

    status = sandbox.run(
        ["git", "status", "--porcelain"],
        cwd=worktree,
        scope="quality",
        writable_cwd=True,
    )
    assert status.returncode == 0, status.stdout

    common_git_dir = container_path_for(str((repo / ".git").resolve()))
    write_attempt = sandbox.run(
        [
            "/bin/sh",
            "-lc",
            f"echo blocked >> '{common_git_dir}/converge-probe' && exit 1 || exit 0",
        ],
        cwd=worktree,
        scope="quality",
        writable_cwd=True,
    )
    assert write_attempt.returncode == 0, write_attempt.stdout
