"""Pin REQ-TOOL-001: read-only roles fail closed on mutating connector tool grants.

REQ-TOOL-001 (CONVERGE_ORCHESTRATOR_2_0_SOURCE_OF_TRUTH.md:L262
[7. Deterministic architecture and agent roles]): A role declared read-only SHALL
receive only read-only effective capabilities across filesystem, Git, GitHub, MCP,
API, database, cloud, deployment, and other external tools even when an underlying
connector also exposes mutating operations.

These deterministic offline assertions lock the OpenCode config builder so that
read-only built-in roles (everything except ``builder``) and declared specialty
analyzers cannot be granted mutating connector families (``github_*``, ``deploy_*``,
``db_*``, ``aws_*``, ``api_*``, ``docker_*``) with ``allow`` or ``ask``.  Read-only
MCP grants such as ``docs_*: allow`` and explicit ``deny`` values for mutating
families remain accepted.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from converge_orchestrator.models import AgentConfig, ProjectConfig
from converge_orchestrator.opencode_config import build_opencode_config


def _project_config(
    tmp_path: Path,
    planner_tools: dict[str, Any] | None = None,
    mcp_servers: dict[str, Any] | None = None,
) -> ProjectConfig:
    repo = tmp_path / "repo"
    repo.mkdir()
    requirements = tmp_path / "architecture.md"
    requirements.write_text("System must remain testable.\n", encoding="utf-8")
    return ProjectConfig(
        repo_path=repo,
        requirements_path=requirements,
        agents={
            "planner": AgentConfig(
                agent="converge-planner",
                model="openai/test",
                tool_permissions=planner_tools or {},
            )
        },
        mcp={"servers": mcp_servers or {}},
    )


def test_req_tool_001_planner_github_allow_raises_fail_closed(tmp_path: Path) -> None:
    """A read-only role granted github_*: allow must fail config build, not receive it."""
    cfg = _project_config(
        tmp_path,
        planner_tools={"github_*": "allow"},
    )
    try:
        build_opencode_config(cfg)
    except ValueError as exc:
        assert "mutating connector" in str(exc), str(exc)
    else:
        pytest.fail(
            "REQ-TOOL-001 gap: read-only role planner still receives mutating "
            "connector grant github_*"
        )


def test_req_tool_001_planner_docs_allow_remains_enabled(tmp_path: Path) -> None:
    """Read-only grants such as docs_*: allow remain accepted and keep docs MCP on."""
    cfg = _project_config(
        tmp_path,
        planner_tools={"docs_*": "allow"},
        mcp_servers={
            "docs": {
                "type": "remote",
                "url": "https://mcp.example.invalid/mcp",
                "enabled": True,
                "oauth": False,
            }
        },
    )
    payload = build_opencode_config(cfg, active_role="planner")

    assert payload["agent"]["converge-planner"]["permission"]["docs_*"] == "allow"
    assert payload["mcp"]["docs"]["enabled"] is True


def test_req_tool_001_planner_github_deny_remains_accepted(tmp_path: Path) -> None:
    """Deny-only declarations for mutating connector families stay accepted."""
    cfg = _project_config(
        tmp_path,
        planner_tools={"github_*": "deny"},
    )
    payload = build_opencode_config(cfg, active_role="planner")

    assert payload["agent"]["converge-planner"]["permission"]["github_*"] == "deny"
