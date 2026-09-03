from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch

from typer.testing import CliRunner

from converge_orchestrator.cli import app
from converge_orchestrator.model_gateway import (
    ModelGatewayError,
    configured_gateway_model_ids,
    gateway_model_ids,
)
from converge_orchestrator.models import ProjectConfig


class _Response:
    def __init__(self, payload: dict) -> None:
        self.payload = payload

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        return None

    def read(self) -> bytes:
        return json.dumps(self.payload).encode("utf-8")


def _config(tmp_path: Path) -> ProjectConfig:
    repo = tmp_path / "repository"
    repo.mkdir()
    requirements = tmp_path / "architecture.md"
    requirements.write_text("System must remain testable.\n", encoding="utf-8")
    return ProjectConfig.model_validate(
        {
            "project": {
                "repo_path": str(repo),
                "requirements_path": str(requirements),
                "require_spec_read_only": False,
            },
            "models": {
                "gateway": {
                    "kind": "openwebui",
                    "base_url": "http://127.0.0.1:3000/api",
                    "api_key_env": "OPENWEBUI_API_KEY",
                },
                "profiles": {
                    "planner": {"model": "deepseek-v4-pro:cloud"},
                    "builder": {"model": "kimi-k2.7-code:cloud"},
                    "unused": {"model": "unused-model"},
                },
            },
            "agents": {
                "planner": {
                    "agent": "converge-planner",
                    "model_profile": "planner",
                },
                "builder": {
                    "agent": "converge-builder",
                    "model_profile": "builder",
                },
            },
        }
    )


def test_gateway_model_ids_uses_openwebui_models_endpoint_and_bearer_token(
    tmp_path: Path,
    monkeypatch,
) -> None:
    cfg = _config(tmp_path)
    monkeypatch.setenv("OPENWEBUI_API_KEY", "secret-token")
    payload = {
        "data": [
            {"id": "deepseek-v4-pro:cloud"},
            {"id": "kimi-k2.7-code:cloud"},
            {"name": "ignored-without-id"},
        ]
    }

    def fake_urlopen(request, timeout):
        assert request.full_url == "http://127.0.0.1:3000/api/models"
        assert request.get_header("Authorization") == "Bearer secret-token"
        assert timeout == 10
        return _Response(payload)

    with patch("converge_orchestrator.model_gateway.urlopen", side_effect=fake_urlopen):
        assert gateway_model_ids(cfg) == {
            "deepseek-v4-pro:cloud",
            "kimi-k2.7-code:cloud",
        }


def test_gateway_requires_configured_secret(tmp_path: Path, monkeypatch) -> None:
    cfg = _config(tmp_path)
    monkeypatch.delenv("OPENWEBUI_API_KEY", raising=False)
    try:
        gateway_model_ids(cfg)
    except ModelGatewayError as exc:
        assert "OPENWEBUI_API_KEY" in str(exc)
    else:
        raise AssertionError("gateway_model_ids should reject a missing gateway secret")


def test_configured_gateway_ids_only_include_active_agent_models(tmp_path: Path) -> None:
    cfg = _config(tmp_path)
    assert configured_gateway_model_ids(cfg) == {
        "deepseek-v4-pro:cloud",
        "kimi-k2.7-code:cloud",
    }


def test_models_cli_lists_sorted_gateway_ids(tmp_path: Path, monkeypatch) -> None:
    cfg = _config(tmp_path)
    config_path = tmp_path / "converge.yaml"
    config_path.write_text(
        f"""
version: 1
project:
  repo_path: {cfg.repo_path}
  requirements_path: {cfg.requirements_path}
  require_spec_read_only: false
models:
  gateway:
    kind: openwebui
    base_url: http://127.0.0.1:3000/api
    api_key_env: OPENWEBUI_API_KEY
  profiles:
    planner:
      model: deepseek-v4-pro:cloud
agents:
  planner:
    agent: converge-planner
    model_profile: planner
""".strip()
        + "\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("OPENWEBUI_API_KEY", "secret-token")
    runner = CliRunner()
    with patch(
        "converge_orchestrator.cli.gateway_model_ids",
        return_value={"z-model", "a-model"},
    ):
        result = runner.invoke(app, ["models", "--config", str(config_path)])

    assert result.exit_code == 0
    assert "gateway: http://127.0.0.1:3000/api" in result.stdout
    assert result.stdout.index("a-model") < result.stdout.index("z-model")
