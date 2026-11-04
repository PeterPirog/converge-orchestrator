"""Pin REQ-AGENT-002: built-in scout and planner roles are enforced read-only.

REQ-AGENT-002 (CONVERGE_ORCHESTRATOR_2_0_SOURCE_OF_TRUTH.md:L246
[7. Deterministic architecture and agent roles]): Repository reconnaissance and planning
roles SHALL be read-only.

These deterministic offline assertions lock the permission layer and prompt layer for
the two built-in read-only roles. No network, model, provider, git command, subprocess
call, or filesystem mutation is performed.
"""

from __future__ import annotations

from typing import Any

import pytest

from converge_orchestrator.models import AgentConfig
from converge_orchestrator.opencode_config import _ROLE_DEFINITIONS, _read_only_permission

_READ_ONLY_ROLES = ("scout", "planner")

_READ_ONLY_PROMPT_DIRECTIVES = {
    "scout": "without modifying files",
    "planner": "Never write code",
}

_READ_ONLY_BASH_ALLOWLIST = {
    "git status",
    "git status *",
    "git diff",
    "git diff *",
    "git log",
    "git log *",
}

_PROTECTED_WRITE_PERMISSIONS = ("edit", "bash", "task", "external_directory")


def _permission(role: str, **agent_kwargs: Any) -> dict[str, Any]:
    return _read_only_permission(role, AgentConfig(agent=f"converge-{role}", **agent_kwargs))


@pytest.mark.parametrize("role", _READ_ONLY_ROLES)
def test_req_agent_002_role_permission_is_read_only(role: str) -> None:
    """Read-only roles deny mutation tools and allow only inspection tools."""
    permission = _permission(role)

    assert permission["*"] == "deny"
    assert permission["edit"] == "deny"
    assert permission["task"] == "deny"
    assert permission["external_directory"] == "deny"
    assert permission["question"] == "deny"

    assert permission["read"] == "allow"
    assert permission["glob"] == "allow"
    assert permission["grep"] == "allow"
    assert permission["list"] == "allow"
    assert permission["lsp"] == "allow"
    assert permission["todowrite"] == "allow"

    bash = permission["bash"]
    assert isinstance(bash, dict)
    assert bash["*"] == "deny"
    assert set(bash) == {"*"} | _READ_ONLY_BASH_ALLOWLIST
    for command in _READ_ONLY_BASH_ALLOWLIST:
        assert bash[command] == "allow", f"{role} bash allowlist must allow {command!r}"

    skill = permission["skill"]
    assert isinstance(skill, dict)
    assert skill["*"] == "deny"
    assert all(
        key == "*" or value == "allow" for key, value in skill.items()
    ), f"{role} skill allowlist must only allow built-in skills"


@pytest.mark.parametrize("role", _READ_ONLY_ROLES)
def test_req_agent_002_role_prompt_forbids_writes(role: str) -> None:
    """Read-only role prompts carry the exact read-only directives."""
    prompt = _ROLE_DEFINITIONS[role]["prompt"]
    directive = _READ_ONLY_PROMPT_DIRECTIVES[role]
    assert directive in prompt, (
        f"REQ-AGENT-002 {role} prompt must contain the read-only directive {directive!r}"
    )


@pytest.mark.parametrize("role", _READ_ONLY_ROLES)
@pytest.mark.parametrize("protected", _PROTECTED_WRITE_PERMISSIONS)
def test_req_agent_002_tool_permissions_cannot_weaken_read_only(role: str, protected: str) -> None:
    """Operator tool_permissions cannot override protected write tooling for read-only roles."""
    with pytest.raises(ValueError, match="custom/MCP tools"):
        _permission(role, tool_permissions={protected: "allow"})
