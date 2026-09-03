from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from .models import AgentConfig, ModelProfile, ProjectConfig


def resolve_profile_model(config: ProjectConfig, profile: ModelProfile) -> str:
    """Resolve a profile to OpenCode's provider/model reference."""
    if "/" in profile.model and profile.provider is None:
        return profile.model
    provider = profile.provider
    if provider is None and config.model_gateway.kind != "existing":
        provider = config.model_gateway.provider_id
    if not provider:
        raise ValueError(
            f"model {profile.model!r} needs provider or a configured model gateway"
        )
    return f"{provider}/{profile.model}"


def resolve_agent_model(config: ProjectConfig, agent: AgentConfig) -> str | None:
    if agent.model_profile:
        return resolve_profile_model(config, config.model_profiles[agent.model_profile])
    return agent.model


def resolve_agent_variant(config: ProjectConfig, agent: AgentConfig) -> str | None:
    if agent.variant:
        return agent.variant
    if agent.model_profile:
        return config.model_profiles[agent.model_profile].variant
    return None


def _agent_request(config: ProjectConfig, agent: AgentConfig) -> dict[str, Any]:
    body: dict[str, Any] = {}
    headers: dict[str, str] = {}
    if agent.model_profile:
        profile = config.model_profiles[agent.model_profile]
        body.update(profile.request_body)
        headers.update(profile.request_headers)
    body.update(agent.request_body)
    headers.update(agent.request_headers)

    request: dict[str, Any] = {}
    if body:
        request["body"] = body
    if headers:
        request["headers"] = headers
    return request


def build_opencode_config(config: ProjectConfig) -> dict[str, Any]:
    """Build OpenCode V2 config without materializing any secret value."""
    payload: dict[str, Any] = {"$schema": "https://opencode.ai/config.json"}
    gateway = config.model_gateway

    if gateway.kind != "existing":
        provider_models: dict[str, dict[str, str]] = {}
        for profile in config.model_profiles.values():
            resolved = resolve_profile_model(config, profile)
            provider, model_id = resolved.split("/", 1)
            if provider != gateway.provider_id:
                continue
            provider_models.setdefault(
                model_id,
                {"name": profile.name or model_id},
            )

        provider: dict[str, Any] = {
            "name": gateway.name,
            "package": gateway.package,
            "settings": {"baseURL": gateway.base_url},
            "models": provider_models,
        }
        if gateway.api_key_env:
            provider["env"] = [gateway.api_key_env]
        if gateway.headers:
            provider["headers"] = gateway.headers
        payload["providers"] = {gateway.provider_id: provider}

    if config.mcp:
        payload["mcp"] = config.mcp

    agent_overrides: dict[str, dict[str, Any]] = {}
    for role, agent in config.agents.items():
        model = resolve_agent_model(config, agent)
        variant = resolve_agent_variant(config, agent)
        override: dict[str, Any] = {}
        if model:
            override["model"] = f"{model}#{variant}" if variant else model
        if agent.steps:
            override["steps"] = agent.steps
        request = _agent_request(config, agent)
        if request:
            override["request"] = request
        if override:
            agent_overrides[agent.agent] = override
        if not agent.agent:
            raise ValueError(f"agent role {role!r} has empty OpenCode agent id")
    if agent_overrides:
        payload["agents"] = agent_overrides
    return payload


def materialize_opencode_config(config: ProjectConfig) -> Path:
    """Write deterministic generated config under state_dir, never into the target repo."""
    target = config.opencode_generated_config_path
    target.parent.mkdir(parents=True, exist_ok=True)
    payload = build_opencode_config(config)
    target.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return target
