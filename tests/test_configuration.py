from __future__ import annotations

import json
from pathlib import Path

import yaml

from converge_orchestrator.models import ProjectConfig
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
            "mcp": {"servers": {}},
        },
        "models": {
            "gateway": {
                "kind": "openwebui",
                "base_url": "http://127.0.0.1:3000/api",
                "api_key_env": "OPENWEBUI_API_KEY",
            },
            "profiles": {
                "planner": {
                    "model": "reasoning-model",
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
    raw = _nested_config(tmp_path)
    cfg = ProjectConfig.model_validate(raw)

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


def test_generated_opencode_config_contains_gateway_agents_and_mcp_without_secret(
    tmp_path: Path,
    monkeypatch,
) -> None:
    monkeypatch.setenv("OPENWEBUI_API_KEY", "must-never-be-written")
    cfg = ProjectConfig.model_validate(_nested_config(tmp_path))
    payload = build_opencode_config(cfg)

    provider = payload["providers"]["openwebui"]
    assert provider["settings"]["baseURL"] == "http://127.0.0.1:3000/api"
    assert provider["env"] == ["OPENWEBUI_API_KEY"]
    assert set(provider["models"]) == {"reasoning-model", "coding-model"}
    assert payload["mcp"] == {"servers": {}}
    assert payload["agents"]["converge-planner"]["model"] == "openwebui/reasoning-model"
    assert payload["agents"]["converge-planner"]["steps"] == 12
    assert payload["agents"]["converge-planner"]["request"]["body"] == {
        "temperature": 0.1
    }
    assert "must-never-be-written" not in json.dumps(payload)

    generated = materialize_opencode_config(cfg)
    assert generated == cfg.opencode_generated_config_path
    assert "must-never-be-written" not in generated.read_text(encoding="utf-8")


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
    assert raw["project"]["repo_path"]
    assert raw["models"]["gateway"]["kind"] == "openwebui"
    assert set(raw["agents"]) == {"planner", "builder", "reviewer"}
