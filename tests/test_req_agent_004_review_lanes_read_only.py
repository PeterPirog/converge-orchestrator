"""Pin REQ-AGENT-004: every configured review lane is enforced read-only.

REQ-AGENT-004 (CONVERGE_ORCHESTRATOR_2_0_SOURCE_OF_TRUTH.md:L250
[7. Deterministic architecture and agent roles]): Every configured review lane SHALL be read-only.

Deterministic offline assertions lock the permission, prompt, operator-override, adapter sandbox,
config validation, and final-audit role surfaces. No network/model/git/subprocess/filesystem
mutation is performed.
"""

from __future__ import annotations

import json
import types
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest

from converge_orchestrator.models import _REVIEW_AGENT_ROLES, AgentConfig, ProjectConfig
from converge_orchestrator.opencode import OpenCodeAdapter
from converge_orchestrator.opencode_config import _ROLE_DEFINITIONS, _role_permission

_REVIEW_ROLES = sorted(_REVIEW_AGENT_ROLES)
_READ_ONLY_BASH_ALLOWLIST = {
    "git status", "git status *", "git diff", "git diff *", "git log", "git log *"
}
_READ_ONLY_PROMPT_DIRECTIVES = ("Do not edit files", "Do not modify files", "Stay read-only")
_PROTECTED_WRITE_PERMISSIONS = ("edit", "bash", "task", "external_directory")
_FINAL_AUDIT_LANE_ROLES = {"architecture_reviewer", "correctness_reviewer", "security_reviewer"}


def _permission(role: str, **agent_kwargs: Any) -> dict[str, Any]:
    return _role_permission(role, AgentConfig(agent=f"converge-{role}", **agent_kwargs))


@pytest.mark.parametrize("role", _REVIEW_ROLES)
def test_req_agent_004_role_permission_is_read_only(role: str) -> None:
    permission = _permission(role)
    assert permission["*"] == "deny"
    for tool in ("edit", "task", "external_directory", "question"):
        assert permission[tool] == "deny"
    for tool in ("read", "glob", "grep", "list", "lsp", "todowrite"):
        assert permission[tool] == "allow"
    bash = permission["bash"]
    assert isinstance(bash, dict) and bash["*"] == "deny"
    assert set(bash) == {"*"} | _READ_ONLY_BASH_ALLOWLIST
    for command in _READ_ONLY_BASH_ALLOWLIST:
        assert bash[command] == "allow"
    skill = permission["skill"]
    assert isinstance(skill, dict) and skill["*"] == "deny"
    assert all(key == "*" or value == "allow" for key, value in skill.items())


@pytest.mark.parametrize("role", _REVIEW_ROLES)
def test_req_agent_004_role_prompt_carries_read_only_directive(role: str) -> None:
    prompt = _ROLE_DEFINITIONS[role]["prompt"]
    assert any(directive in prompt for directive in _READ_ONLY_PROMPT_DIRECTIVES)


@pytest.mark.parametrize("role", _REVIEW_ROLES)
@pytest.mark.parametrize("protected", _PROTECTED_WRITE_PERMISSIONS)
def test_req_agent_004_tool_permissions_cannot_weaken_read_only(role: str, protected: str) -> None:
    with pytest.raises(ValueError, match="custom/MCP tools"):
        _permission(role, tool_permissions={protected: "allow"})


def _config(tmp_path: Path) -> ProjectConfig:
    repo = tmp_path / "repo"
    repo.mkdir(exist_ok=True)
    req = tmp_path / "architecture.md"
    req.write_text("System must remain correct.\n", encoding="utf-8")
    return ProjectConfig(
        repo_path=repo,
        requirements_path=req,
        require_spec_read_only=False,
        review_roles=list(_REVIEW_ROLES),
        agents={
            role: AgentConfig(agent=f"converge-{role.replace('_', '-')}") for role in _REVIEW_ROLES
        },
    )


def test_req_agent_004_adapter_passes_writable_cwd_false_for_every_review_lane(
    tmp_path: Path,
) -> None:
    cfg = _config(tmp_path)
    adapter = OpenCodeAdapter(cfg)
    payload = json.dumps({"verdict": "pass", "findings": [], "confidence": 0.9})

    def fake_run(command, **kwargs):
        del command
        return types.SimpleNamespace(returncode=0, stdout=payload)

    target = "converge_orchestrator.opencode.ExecutionSandbox.run"
    with patch(target, side_effect=fake_run) as runner:
        result = adapter.invoke("reviewer", "Review this diff", cfg.repo_path)
    assert result.ok
    assert len(runner.call_args_list) == len(_REVIEW_ROLES)
    for call in runner.call_args_list:
        assert call.kwargs["writable_cwd"] is False
        assert call.kwargs["agent_role"] in _REVIEW_AGENT_ROLES


def test_req_agent_004_review_roles_reject_builder_writer_lane(tmp_path: Path) -> None:
    base = _config(tmp_path)
    with pytest.raises(ValueError, match="unsupported review roles"):
        ProjectConfig(
            repo_path=base.repo_path,
            requirements_path=base.requirements_path,
            require_spec_read_only=False,
            review_roles=["builder"],
            agents={"builder": AgentConfig(agent="converge-builder")},
        )


def test_req_agent_004_final_audit_lanes_stay_within_review_role_set() -> None:
    assert _FINAL_AUDIT_LANE_ROLES <= _REVIEW_AGENT_ROLES
