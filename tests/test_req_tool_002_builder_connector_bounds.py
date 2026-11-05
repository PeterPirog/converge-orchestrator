"""Pin REQ-TOOL-002: builder fail-closed on mutating connector tool grants.

REQ-TOOL-002 (CONVERGE_ORCHESTRATOR_2_0_SOURCE_OF_TRUTH.md:L263
[7. Deterministic architecture and agent roles]): The autonomous Builder SHALL
NOT receive external capabilities that can push, merge, administer repository
policy, alter deployment state, change credentials, mutate compliance, or
perform another control-plane side effect outside its authorized candidate
workspace and scratch scope.

These deterministic offline assertions extend the REQ-TOOL-001 fail-closed
mechanism to the ``builder`` role: mutating connector families
(``github_*``, ``deploy_*``, ``db_*``, ``aws_*``, ``api_*``, ``docker_*``)
with ``allow`` or ``ask`` raise ValueError.  Read-only MCP grants such as
``docs_*: allow`` and explicit ``deny`` values for mutating families remain
accepted.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from converge_orchestrator.models import AgentConfig, ProjectConfig
from converge_orchestrator.opencode_config import build_opencode_config


def _project_config(
    tmp_path: Path,
    builder_tools: dict[str, Any] | None = None,
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
            "builder": AgentConfig(
                agent="converge-builder",
                model="openai/test",
                tool_permissions=builder_tools or {},
            )
        },
        mcp={"servers": mcp_servers or {}},
    )


def test_req_tool_002_builder_github_allow_raises_fail_closed(tmp_path: Path) -> None:
    """A builder granted github_*: allow must fail config build, not receive it."""
    cfg = _project_config(
        tmp_path,
        builder_tools={"github_*": "allow"},
    )
    try:
        build_opencode_config(cfg)
    except ValueError as exc:
        assert "mutating connector" in str(exc), str(exc)
    else:
        pytest.fail(
            "REQ-TOOL-002 gap: builder still receives mutating connector grant"
        )


def test_req_tool_002_builder_docs_allow_remains_enabled(tmp_path: Path) -> None:
    """Read-only grants such as docs_*: allow remain accepted and keep docs MCP on."""
    cfg = _project_config(
        tmp_path,
        builder_tools={"docs_*": "allow"},
        mcp_servers={
            "docs": {
                "type": "remote",
                "url": "https://mcp.example.invalid/mcp",
                "enabled": True,
                "oauth": False,
            }
        },
    )
    payload = build_opencode_config(cfg, active_role="builder")

    assert payload["agent"]["converge-builder"]["permission"]["docs_*"] == "allow"
    assert payload["mcp"]["docs"]["enabled"] is True


def test_req_tool_002_builder_github_deny_remains_accepted(tmp_path: Path) -> None:
    """Deny-only declarations for mutating connector families stay accepted."""
    cfg = _project_config(
        tmp_path,
        builder_tools={"github_*": "deny"},
    )
    payload = build_opencode_config(cfg, active_role="builder")

    assert payload["agent"]["converge-builder"]["permission"]["github_*"] == "deny"
