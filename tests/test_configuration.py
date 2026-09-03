from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch

import pytest
import yaml
from pydantic import ValidationError

from converge_orchestrator.config import load_config
from converge_orchestrator.models import ProjectConfig
from converge_orchestrator.opencode import OpenCodeAdapter
from converge_orchestrator.opencode_config import (
    build_opencode_config,
    materialize_opencode_config,
    resolve_agent_model,
)


def _nested_config(tmp_path: Path) -> dict:
    repo = tmp_path / "repository"
    repo.mkdir()
    requirements = tmp_path / "architecture.md"
    requirements.write_text("System must remain testable.\n", encoding="utf-8")
    return {
        "version": 1,
        "project": {
            "name": "fixture",
            "repo_path": str(repo),
            "requirements_path": str(requirements),
            "state_dir": None,
            "worktree_dir": None,
            "require_spec_read_only": False,
        },
        "github": {
            "repo": "acme/fixture",
            "base_branch": "main",
            "branch_prefix": "converge/",
            "auto_merge": False,
        },
        "opencode": {
            "binary": "opencode",
            "attach_url": None,
            "auto_approve": True,
            "generated_config_path": None,
            "mcp": {
                "servers": {
                    "docs": {
                        "type": "remote",
                        "url": "https://mcp.example.invalid/mcp",
                        "enabled": True,
                        "oauth": False,
                        "headers": {"X-API-Key": "{env:DOCS_MCP_API_KEY}"},
                    }
                }
            },
        },
        "models": {
            "gateway": {
                "kind": "openwebui",
                "base_url": "http://127.0.0.1:3000/api",
                "api_key_env": "OPENWEBUI_API_KEY",
            },
            "profiles": {
                "planner": {
                    "model": "reasoning/model",
                    "request_body": {"temperature": 0.1},
                },
                "builder": {
                    "model": "coding-model",
                    "request_body": {"temperature": 0.0},
                },
            },
        },
        "agents": {
            "planner": {
                "agent": "converge-planner",
                "model_profile": "planner",
                "steps": 12,
                "tool_permissions": {"docs_*": "allow"},
            },
            "builder": {
                "agent": "converge-builder",
                "model_profile": "builder",
                "steps": 40,
            },
        },
        "quality": {
            "auto_discover": True,
            "gates": [],
            "requirement_verifiers": {},
        },
        "workflow": {
            "max_repair_attempts": 3,
            "max_replans": 2,
            "max_iterations": 50,
            "max_diff_lines_hard": 1000,
        },
    }


def test_documented_nested_config_flattens_to_runtime_model(tmp_path: Path) -> None:
    cfg = ProjectConfig.model_validate(_nested_config(tmp_path))

    assert cfg.version == 1
    assert cfg.project_name == "fixture"
    assert cfg.github_repo == "acme/fixture"
    assert cfg.repo_path == (tmp_path / "repository").resolve()
    assert cfg.state_dir == (tmp_path / ".converge").resolve()
    assert cfg.worktree_dir == (tmp_path / ".converge" / "worktrees").resolve()
    assert cfg.opencode_generated_config_path == (
        tmp_path / ".converge" / "opencode.generated.json"
    ).resolve()
    assert cfg.model_gateway.kind == "openwebui"
    assert cfg.agents["builder"].model_profile == "builder"
    assert resolve_agent_model(cfg, cfg.agents["builder"]) == "openwebui/coding-model"
    assert resolve_agent_model(cfg, cfg.agents["planner"]) == "openwebui/reasoning/model"


def test_generated_stable_opencode_config_contains_gateway_agents_mcp_and_safety(
    tmp_path: Path,
    monkeypatch,
) -> None:
    monkeypatch.setenv("OPENWEBUI_API_KEY", "must-never-be-written")
    cfg = ProjectConfig.model_validate(_nested_config(tmp_path))
    payload = build_opencode_config(cfg)

    provider = payload["provider"]["openwebui"]
    assert provider["npm"] == "@ai-sdk/openai-compatible"
    assert provider["options"]["baseURL"] == "http://127.0.0.1:3000/api"
    assert provider["options"]["apiKey"] == "{env:OPENWEBUI_API_KEY}"
    assert set(provider["models"]) == {"reasoning/model", "coding-model"}
    assert payload["mcp"]["docs"]["enabled"] is True
    assert payload["mcp"]["docs"]["oauth"] is False

    planner = payload["agent"]["converge-planner"]
    builder = payload["agent"]["converge-builder"]
    assert planner["model"] == "openwebui/reasoning/model"
    assert planner["steps"] == 12
    assert planner["temperature"] == 0.1
    assert planner["permission"]["edit"] == "deny"
    assert planner["permission"]["docs_*"] == "allow"
    assert builder["permission"]["edit"] == "allow"
    assert builder["permission"]["bash"]["git push *"] == "deny"
    assert builder["permission"]["external_directory"] == "deny"
    assert "must-never-be-written" not in json.dumps(payload)

    generated = materialize_opencode_config(cfg)
    assert generated == cfg.opencode_generated_config_path
    assert "must-never-be-written" not in generated.read_text(encoding="utf-8")


def test_request_body_cannot_override_orchestrator_safety_fields(tmp_path: Path) -> None:
    raw = _nested_config(tmp_path)
    raw["agents"]["builder"]["request_body"] = {"permission": {"bash": "allow"}}
    cfg = ProjectConfig.model_validate(raw)
    with pytest.raises(ValueError, match="safety fields"):
        build_opencode_config(cfg)


def test_tool_permissions_cannot_weaken_core_role_boundaries(tmp_path: Path) -> None:
    raw = _nested_config(tmp_path)
    raw["agents"]["planner"]["tool_permissions"] = {"edit": "allow"}
    cfg = ProjectConfig.model_validate(raw)
    with pytest.raises(ValueError, match="custom/MCP tools"):
        build_opencode_config(cfg)


def test_opencode_adapter_sets_high_precedence_inline_runtime_config(tmp_path: Path) -> None:
    cfg = ProjectConfig.model_validate(_nested_config(tmp_path))
    adapter = OpenCodeAdapter(cfg)
    fake_result = type("Result", (), {"returncode": 0, "stdout": "ok"})()

    with patch("converge_orchestrator.opencode.run", return_value=fake_result) as runner:
        result = adapter.invoke("builder", "Implement task", cfg.repo_path)

    assert result.ok
    call = runner.call_args
    env = call.kwargs["env"]
    inline = json.loads(env["OPENCODE_CONFIG_CONTENT"])
    assert env["OPENCODE_CONFIG"] == str(cfg.opencode_generated_config_path)
    assert inline["agent"]["converge-builder"]["permission"]["bash"]["gh *"] == "deny"
    command = call.args[0]
    assert "--auto" in command
    assert command[command.index("--agent") + 1] == "converge-builder"


def test_load_config_resolves_relative_paths_from_yaml_directory(tmp_path: Path) -> None:
    project = tmp_path / "project"
    project.mkdir()
    repo = project / "repository"
    repo.mkdir()
    requirements = project / "architecture.md"
    requirements.write_text("System must remain stable.\n", encoding="utf-8")
    config_path = project / "converge.yaml"
    config_path.write_text(
        """
version: 1
project:
  repo_path: ./repository
  requirements_path: ./architecture.md
  state_dir: ./.state
  require_spec_read_only: false
opencode:
  generated_config_path: ./.runtime/opencode.json
agents:
  planner:
    agent: converge-planner
    model: openai/example
""".strip()
        + "\n",
        encoding="utf-8",
    )

    cfg = load_config(config_path)
    assert cfg.repo_path == repo.resolve()
    assert cfg.requirements_path == requirements.resolve()
    assert cfg.state_dir == (project / ".state").resolve()
    assert cfg.opencode_generated_config_path == (project / ".runtime/opencode.json").resolve()


def test_unknown_config_version_is_rejected(tmp_path: Path) -> None:
    raw = _nested_config(tmp_path)
    raw["version"] = 2
    with pytest.raises(ValidationError):
        ProjectConfig.model_validate(raw)


def test_legacy_flat_configuration_remains_supported(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    requirements = tmp_path / "architecture.md"
    requirements.write_text("System must remain stable.\n", encoding="utf-8")
    cfg = ProjectConfig(
        repo_path=repo,
        requirements_path=requirements,
        agents={"planner": {"agent": "converge-planner", "model": "openai/gpt-test"}},
    )
    assert cfg.repo_path == repo.resolve()
    assert cfg.model_gateway.kind == "existing"
    assert resolve_agent_model(cfg, cfg.agents["planner"]) == "openai/gpt-test"


def test_example_yaml_is_valid_single_file_configuration() -> None:
    source = Path("examples/converge.yaml").read_text(encoding="utf-8")
    raw = yaml.safe_load(source)
    assert raw["version"] == 1
    assert raw["project"]["repo_path"]
    assert raw["opencode"]["binary"] == "opencode"
    assert raw["models"]["gateway"]["kind"] == "openwebui"
    assert raw["models"]["profiles"]["planner"]["model"] == "deepseek-v4-pro:cloud"
    assert raw["models"]["profiles"]["builder"]["model"] == "kimi-k2.7-code:cloud"
    assert raw["models"]["profiles"]["reviewer"]["model"] == "glm-5.3-flash:cloud"
    assert raw["models"]["profiles"]["planner"]["request_body"] == {}
    assert raw["models"]["profiles"]["builder"]["request_body"] == {}
    assert raw["models"]["profiles"]["reviewer"]["request_body"] == {}
    assert raw["agents"]["planner"]["steps"] == 18
    assert raw["agents"]["builder"]["steps"] == 60
    assert raw["agents"]["reviewer"]["steps"] == 24
    assert set(raw["agents"]) == {"planner", "builder", "reviewer"}
