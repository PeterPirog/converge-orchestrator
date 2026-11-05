"""REQ-RUNTIME-001: AgentRuntime boundary Protocol pins the v1.0 OpenCode adapter contract."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
import yaml

from converge_orchestrator import workflow
from converge_orchestrator.models import (
    AgentConfig,
    AgentResult,
    ModelProfile,
    ProjectConfig,
    TaskEnvelope,
)
from converge_orchestrator.opencode import AgentRuntime, OpenCodeAdapter

_REQUIREMENTS_TEXT = "The orchestrator converges the repository against immutable requirements.\n"


def _minimal_config(tmp_path: Path) -> Path:
    repo = tmp_path / "repository"
    repo.mkdir(parents=True, exist_ok=True)
    state = tmp_path / "state"
    worktrees = state / "worktrees"
    worktrees.mkdir(parents=True, exist_ok=True)
    requirements = tmp_path / "requirements.md"
    requirements.write_text(_REQUIREMENTS_TEXT, encoding="utf-8")
    config_path = tmp_path / "converge.yaml"
    config_path.write_text(
        yaml.safe_dump(
            {
                "version": 1,
                "project": {
                    "name": "boundary-test",
                    "repo_path": str(repo),
                    "requirements_path": str(requirements),
                    "state_dir": str(state),
                    "worktree_dir": str(worktrees),
                    "require_spec_read_only": False,
                },
                "models": {
                    "gateway": {"kind": "existing", "provider_id": "static", "name": "Static"},
                    "profiles": {"primary": {"model": "openai/gpt-4o-mini"}},
                },
                "agents": {"builder": {"agent": "converge-builder", "model_profile": "primary"}},
            },
            sort_keys=True,
        ),
        encoding="utf-8",
    )
    return config_path

class _RecordingStubRuntime:
    def __init__(self, result: AgentResult) -> None:
        self.result = result
        self.calls: list[tuple[str, Any, Path]] = []

    def invoke(self, role: str, prompt: Any, cwd: Path) -> AgentResult:
        self.calls.append((role, prompt, cwd))
        return self.result


def test_opencode_adapter_conforms_to_agent_runtime_protocol(tmp_path: Path) -> None:
    requirements = tmp_path / "requirements.md"
    requirements.write_text(_REQUIREMENTS_TEXT, encoding="utf-8")
    cfg = ProjectConfig(
        repo_path=tmp_path / "repo",
        requirements_path=requirements,
        agents={"builder": AgentConfig(agent="converge-builder", model_profile="primary")},
        model_profiles={"primary": ModelProfile(model="openai/gpt-4o-mini")},
    )
    assert isinstance(OpenCodeAdapter(cfg), AgentRuntime)


@pytest.mark.parametrize(
    "ok,output,returncode,expected_status",
    [
        (True, "stub build output", 0, "built"),
        (False, "stub build failure", 1, "builder_failed"),
    ],
)
def test_alternative_runtime_substitutes_at_workflow_seam(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    ok: bool,
    output: str,
    returncode: int,
    expected_status: str,
) -> None:
    config_path = _minimal_config(tmp_path)
    worktree = tmp_path / "state" / "worktrees" / "task-123"
    worktree.mkdir(parents=True, exist_ok=True)

    task = TaskEnvelope(
        id="task-123",
        requirement_ids=["REQ-001"],
        title="boundary test",
        objective="verify runtime seam substitution",
    )
    context = {"runtime": "stub", "role": "builder", "ok": ok}
    stub = _RecordingStubRuntime(
        AgentResult(role="builder", ok=ok, output=output, returncode=returncode, context=context)
    )
    monkeypatch.setattr(workflow, "OpenCodeAdapter", lambda _cfg: stub)

    state: Any = {
        "config_path": str(config_path),
        "task": task.model_dump(mode="json"),
        "worktree": str(worktree),
        "run_id": "run-001",
        "iteration": 0,
        "status": "planned",
        "requirements": [{"id": "REQ-001", "statement": "test", "source": "requirements.md"}],
    }
    next_state: Any = workflow.build(state)

    assert isinstance(stub, AgentRuntime)
    assert len(stub.calls) == 1
    role, prompt, cwd = stub.calls[0]
    assert role == "builder"
    assert isinstance(prompt, str)
    assert cwd.resolve() == worktree.resolve()
    assert next_state["message"] == output
    assert next_state["status"] == expected_status

    evidence_file = tmp_path / "state" / "evidence" / "run-001" / "task-123" / "context-build.json"
    assert evidence_file.exists()
    assert json.loads(evidence_file.read_text(encoding="utf-8")) == context
