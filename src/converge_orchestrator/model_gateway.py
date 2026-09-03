from __future__ import annotations

import json
import os
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from .models import ProjectConfig
from .opencode_config import resolve_agent_model


class ModelGatewayError(RuntimeError):
    pass


def gateway_model_ids(config: ProjectConfig, timeout_seconds: int = 10) -> set[str]:
    """Read model IDs from an OpenAI-compatible/OpenWebUI gateway catalog."""
    gateway = config.model_gateway
    if gateway.kind == "existing":
        return set()
    if not gateway.base_url:
        raise ModelGatewayError("model gateway has no base_url")

    headers = dict(gateway.headers)
    if gateway.api_key_env:
        token = os.environ.get(gateway.api_key_env)
        if not token:
            raise ModelGatewayError(
                f"missing environment variable {gateway.api_key_env} for model gateway"
            )
        headers.setdefault("Authorization", f"Bearer {token}")
    request = Request(f"{gateway.base_url.rstrip('/')}/models", headers=headers)
    try:
        with urlopen(request, timeout=timeout_seconds) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except (HTTPError, URLError, TimeoutError, json.JSONDecodeError) as exc:
        raise ModelGatewayError(f"unable to query model gateway: {exc}") from exc

    data = payload.get("data", []) if isinstance(payload, dict) else []
    return {
        str(item["id"])
        for item in data
        if isinstance(item, dict) and item.get("id") is not None
    }


def configured_gateway_model_ids(config: ProjectConfig) -> set[str]:
    gateway = config.model_gateway
    if gateway.kind == "existing":
        return set()
    prefix = f"{gateway.provider_id}/"
    configured: set[str] = set()
    for agent in config.agents.values():
        resolved = resolve_agent_model(config, agent)
        if resolved and resolved.startswith(prefix):
            configured.add(resolved[len(prefix) :])
    return configured
