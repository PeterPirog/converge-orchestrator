from __future__ import annotations

import json
import types
from pathlib import Path
from unittest.mock import patch

from converge_orchestrator.managed_skills import materialize_managed_skills
from converge_orchestrator.models import ProjectConfig
from converge_orchestrator.opencode import OpenCodeAdapter
from converge_orchestrator.opencode_config import runtime_opencode_config
from converge_orchestrator.sandbox import ExecutionSandbox


def _config(tmp_path: Path, *, mode: str = "host") -> ProjectConfig:
    repo = tmp_path / "repo"
    repo.mkdir(exist_ok=True)
    (repo / ".git").mkdir(exist_ok=True)
    requirements = tmp_path / "architecture.md"
    requirements.write_text("System must remain isolated.\n", encoding="utf-8")
    sandbox: dict[str, object] = {
        "mode": mode,
        "pass_env": [],
    }
    if mode == "container":
        sandbox.update(
            {
                "image": "converge-runtime:test",
                "agent_network": "converge-ai",
                "agent_gateway_base_url": "http://open-webui:8080/api",
            }
        )
    return ProjectConfig(
        repo_path=repo,
        requirements_path=requirements,
        require_spec_read_only=False,
        model_gateway={
            "kind": "openwebui",
            "base_url": "http://127.0.0.1:3000/api",
            "api_key_env": "OPENWEBUI_API_KEY",
        },
        model_profiles={
            "planner": {"model": "planner-model", "context_tokens": 131072},
            "builder": {"model": "builder-model", "context_tokens": 131072},
        },
        mcp={
            "servers": {
                "docs": {
                    "type": "remote",
                    "url": "https://docs.invalid/mcp",
                    "headers": {"X-API-Key": "{env:DOCS_MCP_KEY}"},
                },
                "deploy": {
                    "type": "remote",
                    "url": "https://deploy.invalid/mcp",
                    "headers": {"Authorization": "Bearer {env:DEPLOY_MCP_KEY}"},
                },
            }
        },
        sandbox=sandbox,
        agents={
            "planner": {
                "agent": "converge-planner",
                "model_profile": "planner",
                "tool_permissions": {"docs_*": "allow"},
            },
            "builder": {
                "agent": "converge-builder",
                "model_profile": "builder",
                "tool_permissions": {"deploy_*": "allow"},
            },
        },
    )


def test_runtime_config_enables_only_role_assigned_mcp_and_skills(tmp_path: Path) -> None:
    cfg = _config(tmp_path)

    planner = runtime_opencode_config(cfg, active_role="planner")
    assert planner["mcp"]["docs"]["enabled"] is True
    assert planner["mcp"]["deploy"]["enabled"] is False
    planner_permissions = planner["agent"]["converge-planner"]["permission"]
    assert planner_permissions["docs_*"] == "allow"
    assert planner_permissions["skill"] == {
        "*": "deny",
        "bounded-planning": "allow",
        "requirements-compliance": "allow",
    }

    builder = runtime_opencode_config(cfg, active_role="builder")
    assert builder["mcp"]["docs"]["enabled"] is False
    assert builder["mcp"]["deploy"]["enabled"] is True
    builder_skills = builder["agent"]["converge-builder"]["permission"]["skill"]
    assert builder_skills == {
        "*": "deny",
        "test-driven-change": "allow",
        "requirements-compliance": "allow",
    }


def test_managed_skills_are_materialized_outside_target_repo(tmp_path: Path) -> None:
    cfg = _config(tmp_path)
    root = materialize_managed_skills(cfg)

    assert root == cfg.state_dir / "opencode-runtime"
    assert not root.is_relative_to(cfg.repo_path)
    assert (root / "skills" / "bounded-planning" / "SKILL.md").is_file()
    assert (root / "skills" / "security-review" / "SKILL.md").is_file()


def test_host_agent_environment_excludes_other_role_and_unrelated_secrets(
    tmp_path: Path,
    monkeypatch,
) -> None:
    cfg = _config(tmp_path)
    monkeypatch.setenv("OPENWEBUI_API_KEY", "gateway-secret")
    monkeypatch.setenv("DOCS_MCP_KEY", "docs-secret")
    monkeypatch.setenv("DEPLOY_MCP_KEY", "deploy-secret")
    monkeypatch.setenv("UNRELATED_PARENT_SECRET", "must-not-leak")
    completed = types.SimpleNamespace(returncode=0, stdout="ok")

    with patch("converge_orchestrator.sandbox.run_configured", return_value=completed) as runner:
        ExecutionSandbox(cfg).run(
            ["opencode", "run"],
            cwd=cfg.repo_path,
            scope="agent",
            agent_role="planner",
            env={"OPENCODE_CONFIG_CONTENT": "{}"},
        )

    call = runner.call_args
    passed = call.kwargs["env"]
    assert call.kwargs["inherit_env"] is False
    assert passed["OPENWEBUI_API_KEY"] == "gateway-secret"
    assert passed["DOCS_MCP_KEY"] == "docs-secret"
    assert "DEPLOY_MCP_KEY" not in passed
    assert "UNRELATED_PARENT_SECRET" not in passed


def test_adapter_does_not_mount_full_state_into_agent_runtime(tmp_path: Path) -> None:
    cfg = _config(tmp_path)
    adapter = OpenCodeAdapter(cfg)
    completed = types.SimpleNamespace(returncode=0, stdout="ok")

    with patch("converge_orchestrator.opencode.ExecutionSandbox.run", return_value=completed) as run:
        result = adapter.invoke("planner", "Plan one task", cfg.repo_path)

    assert result.ok
    call = run.call_args
    assert call.kwargs["include_state"] is False
    assert call.kwargs["agent_role"] == "planner"
    readonly_paths = call.kwargs["readonly_paths"]
    assert cfg.opencode_generated_config_path in readonly_paths
    managed_dir = cfg.state_dir / "opencode-runtime"
    assert managed_dir in readonly_paths
    env = call.kwargs["env"]
    assert env["OPENCODE_CONFIG_DIR"] == str(managed_dir)
    inline = json.loads(env["OPENCODE_CONFIG_CONTENT"])
    assert inline["mcp"]["docs"]["enabled"] is True
    assert inline["mcp"]["deploy"]["enabled"] is False
