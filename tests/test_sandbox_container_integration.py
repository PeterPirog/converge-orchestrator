from __future__ import annotations

import os
from pathlib import Path

import pytest

from converge_orchestrator.models import ProjectConfig
from converge_orchestrator.sandbox import ExecutionSandbox

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
