from __future__ import annotations

import json
from copy import deepcopy
from pathlib import Path
from typing import Any

from .models import AgentConfig, ModelProfile, ProjectConfig

_ROLE_DEFINITIONS: dict[str, dict[str, str]] = {
    "planner": {
        "description": "Plans one minimal architecture-convergence task without modifying code.",
        "prompt": (
            "You are a conservative software architect. Inspect the repository and choose one "
            "small, verifiable change that improves compliance with the supplied immutable "
            "requirements. Never modify architecture requirements. Never write code. Prefer work "
            "that can be accepted by deterministic tests. When requested for JSON, output JSON "
            "only."
        ),
    },
    "builder": {
        "description": "Implements one bounded task with tests inside an isolated worktree.",
        "prompt": (
            "You are the sole writer for the current git worktree. Implement only the assigned "
            "task. Architecture requirements are immutable. Inspect before editing, keep diffs "
            "minimal, add meaningful tests, and preserve observable behavior unless the task "
            "explicitly requires a change. Never push, merge, reset the base branch, or edit the "
            "requirements file."
        ),
    },
    "reviewer": {
        "description": "Independent read-only architecture and quality reviewer.",
        "prompt": (
            "You are independent from the implementation agent. Review the actual diff and "
            "repository context against the immutable requirements. Reject architectural drift, "
            "unnecessary scope, weak tests, unsafe behavior, and accidental API changes. Do not "
            "modify files. When requested for JSON, output JSON only."
        ),
    },
}

_RESERVED_AGENT_OPTIONS = {
    "description",
    "disable",
    "hidden",
    "mode",
    "model",
    "permission",
    "prompt",
    "steps",
    "tools",
}
_PROTECTED_TOOL_PERMISSIONS = {
    "*",
    "bash",
    "doom_loop",
    "edit",
    "external_directory",
    "glob",
    "grep",
    "list",
    "lsp",
    "question",
    "read",
    "skill",
    "task",
    "todowrite",
    "webfetch",
    "websearch",
}


def resolve_profile_model(config: ProjectConfig, profile: ModelProfile) -> str:
    """Resolve a model profile to stable OpenCode's provider/model reference."""
    if profile.provider:
        return f"{profile.provider}/{profile.model}"
    if config.model_gateway.kind != "existing":
        return f"{config.model_gateway.provider_id}/{profile.model}"
    if "/" in profile.model:
        return profile.model
    raise ValueError(
        f"model {profile.model!r} needs provider or a configured model gateway"
    )


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


def _agent_model_options(config: ProjectConfig, agent: AgentConfig) -> dict[str, Any]:
    body: dict[str, Any] = {}
    if agent.model_profile:
        body.update(config.model_profiles[agent.model_profile].request_body)
    body.update(agent.request_body)
    reserved = sorted(_RESERVED_AGENT_OPTIONS.intersection(body))
    if reserved:
        raise ValueError(
            "agent request_body may not override orchestrator safety fields: "
            f"{reserved}"
        )
    return body


def _safe_tool_overrides(agent: AgentConfig) -> dict[str, str]:
    protected = sorted(_PROTECTED_TOOL_PERMISSIONS.intersection(agent.tool_permissions))
    if protected:
        raise ValueError(
            "agent tool_permissions may only target custom/MCP tools; protected keys: "
            f"{protected}"
        )
    return dict(agent.tool_permissions)


def _read_only_permission(agent: AgentConfig) -> dict[str, Any]:
    permission: dict[str, Any] = {
        "*": "deny",
        "read": "allow",
        "glob": "allow",
        "grep": "allow",
        "list": "allow",
        "lsp": "allow",
        "skill": "allow",
        "todowrite": "allow",
        "edit": "deny",
        "bash": {
            "*": "deny",
            "git status": "allow",
            "git status *": "allow",
            "git diff": "allow",
            "git diff *": "allow",
            "git log": "allow",
            "git log *": "allow",
        },
        "task": "deny",
        "external_directory": "deny",
        "question": "deny",
    }
    permission.update(_safe_tool_overrides(agent))
    return permission


def _builder_permission(agent: AgentConfig) -> dict[str, Any]:
    permission: dict[str, Any] = {
        "*": "deny",
        "read": "allow",
        "glob": "allow",
        "grep": "allow",
        "list": "allow",
        "lsp": "allow",
        "skill": "allow",
        "todowrite": "allow",
        "edit": "allow",
        "bash": {
            "*": "allow",
            "git push": "deny",
            "git push *": "deny",
            "git reset --hard": "deny",
            "git reset --hard *": "deny",
            "git clean": "deny",
            "git clean *": "deny",
            "gh": "deny",
            "gh *": "deny",
        },
        "task": "deny",
        "external_directory": "deny",
        "question": "deny",
    }
    permission.update(_safe_tool_overrides(agent))
    return permission


def _role_permission(role: str, agent: AgentConfig) -> dict[str, Any]:
    if role == "builder":
        return _builder_permission(agent)
    return _read_only_permission(agent)


def _stable_mcp_config(raw: dict[str, Any]) -> dict[str, Any]:
    """Accept neutral `mcp.servers` and emit stable OpenCode's `mcp.<name>` shape."""
    if not raw:
        return {}
    servers = raw.get("servers") if isinstance(raw.get("servers"), dict) else raw
    output: dict[str, Any] = {}
    for name, configured in servers.items():
        if not isinstance(configured, dict):
            raise ValueError(f"MCP server {name!r} must be an object")
        server = deepcopy(configured)
        if "disabled" in server:
            server["enabled"] = not bool(server.pop("disabled"))
        server.setdefault("enabled", True)
        timeout = server.get("timeout")
        if isinstance(timeout, dict):
            catalog = timeout.get("catalog")
            if catalog is not None:
                server["timeout"] = catalog
            else:
                server.pop("timeout", None)
        server.pop("codemode", None)
        output[str(name)] = server
    return output


def _provider_model_entry(profile: ModelProfile, model_id: str) -> dict[str, Any]:
    entry: dict[str, Any] = {"name": profile.name or model_id}
    limit: dict[str, int] = {}
    if profile.context_tokens is not None:
        limit["context"] = profile.context_tokens
    if profile.output_tokens is not None:
        limit["output"] = profile.output_tokens
    if limit:
        entry["limit"] = limit
    return entry


def _add_provider_model(
    provider_models: dict[str, dict[str, Any]],
    profile: ModelProfile,
    model_id: str,
) -> None:
    entry = _provider_model_entry(profile, model_id)
    existing = provider_models.get(model_id)
    if existing is None:
        provider_models[model_id] = entry
        return
    existing_limit = existing.get("limit")
    new_limit = entry.get("limit")
    if existing_limit and new_limit and existing_limit != new_limit:
        raise ValueError(
            f"model {model_id!r} has conflicting OpenCode context/output limits"
        )
    if not existing_limit and new_limit:
        existing["limit"] = new_limit


def build_opencode_config(config: ProjectConfig) -> dict[str, Any]:
    """Build stable OpenCode config without materializing any secret value."""
    payload: dict[str, Any] = {"$schema": "https://opencode.ai/config.json"}
    gateway = config.model_gateway

    if gateway.kind != "existing":
        provider_models: dict[str, dict[str, Any]] = {}
        for profile in config.model_profiles.values():
            resolved = resolve_profile_model(config, profile)
            provider, model_id = resolved.split("/", 1)
            if provider != gateway.provider_id:
                continue
            _add_provider_model(provider_models, profile, model_id)

        options: dict[str, Any] = {"baseURL": gateway.base_url}
        if gateway.api_key_env:
            options["apiKey"] = f"{{env:{gateway.api_key_env}}}"
        if gateway.headers:
            options["headers"] = gateway.headers
        provider: dict[str, Any] = {
            "npm": gateway.package,
            "name": gateway.name,
            "options": options,
            "models": provider_models,
        }
        payload["provider"] = {gateway.provider_id: provider}

    mcp = _stable_mcp_config(config.mcp)
    if mcp:
        payload["mcp"] = mcp

    agent_overrides: dict[str, dict[str, Any]] = {}
    for role, agent in config.agents.items():
        if not agent.agent:
            raise ValueError(f"agent role {role!r} has empty OpenCode agent id")
        role_definition = _ROLE_DEFINITIONS.get(role)
        if role_definition is None:
            raise ValueError(
                f"unsupported agent role {role!r}; built-in roles are "
                f"{sorted(_ROLE_DEFINITIONS)}"
            )
        model = resolve_agent_model(config, agent)
        override: dict[str, Any] = {
            "description": role_definition["description"],
            "mode": "all",
            "prompt": role_definition["prompt"],
            "permission": _role_permission(role, agent),
        }
        if model:
            override["model"] = model
        if agent.steps:
            override["steps"] = agent.steps
        override.update(_agent_model_options(config, agent))
        agent_overrides[agent.agent] = override
    if agent_overrides:
        payload["agent"] = agent_overrides
    return payload


def runtime_opencode_config(config: ProjectConfig) -> dict[str, Any]:
    """Return the highest-precedence runtime config used for every local OpenCode call."""
    return build_opencode_config(config)


def materialize_opencode_config(config: ProjectConfig) -> Path:
    """Write deterministic generated config under state_dir, never into the target repo."""
    target = config.opencode_generated_config_path
    target.parent.mkdir(parents=True, exist_ok=True)
    payload = runtime_opencode_config(config)
    target.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return target
