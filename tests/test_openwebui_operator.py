from __future__ import annotations

import asyncio
import importlib.util
from pathlib import Path
from unittest.mock import AsyncMock


def _tools_class():
    source = Path("integrations/openwebui/converge_operator.py")
    spec = importlib.util.spec_from_file_location("converge_openwebui_operator", source)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.Tools


def _run(coro):
    return asyncio.run(coro)


def test_openwebui_operator_valves_keep_token_as_password_input() -> None:
    tools = _tools_class()()
    schema = tools.Valves.model_json_schema()

    token_schema = schema["properties"]["api_token"]
    assert token_schema["input"] == {"type": "password"}
    tools.valves.api_token = "top-secret"
    assert tools._headers() == {"Authorization": "Bearer top-secret"}


def test_mutating_tool_fails_closed_without_confirmation() -> None:
    tools = _tools_class()()
    tools._request = AsyncMock(return_value={"ok": True})

    result = _run(tools.start_project("payments"))

    assert result["ok"] is False
    assert result["cancelled"] is True
    assert "confirmation is unavailable" in result["error"]
    tools._request.assert_not_awaited()


def test_mutating_tool_does_not_execute_when_confirmation_is_denied() -> None:
    tools = _tools_class()()
    tools._request = AsyncMock(return_value={"ok": True})

    async def deny(_event):
        return False

    result = _run(tools.pause_run("run-123", __event_call__=deny))

    assert result["cancelled"] is True
    tools._request.assert_not_awaited()


def test_mutating_tool_treats_event_call_error_as_denial() -> None:
    tools = _tools_class()()
    tools._request = AsyncMock(return_value={"ok": True})

    async def disconnected(_event):
        return {"error": "Client session disconnected."}

    result = _run(tools.resume_run("run-123", __event_call__=disconnected))

    assert result["cancelled"] is True
    assert "disconnected" in result["error"]
    tools._request.assert_not_awaited()


def test_start_project_executes_only_after_explicit_confirmation() -> None:
    tools = _tools_class()()
    tools._request = AsyncMock(
        return_value={"ok": True, "status_code": 202, "data": {"id": "run-123"}}
    )
    confirmations = []

    async def confirm(event):
        confirmations.append(event)
        return True

    result = _run(tools.start_project("payments", __event_call__=confirm))

    assert result["ok"] is True
    assert confirmations[0]["type"] == "confirmation"
    assert "payments" in confirmations[0]["data"]["message"]
    tools._request.assert_awaited_once_with("POST", "/projects/payments/run")


def test_register_project_requires_confirmation_before_sending_config_path() -> None:
    tools = _tools_class()()
    tools._request = AsyncMock(return_value={"ok": True, "data": {"id": "payments"}})

    async def confirm(_event):
        return True

    result = _run(
        tools.register_project(
            "payments",
            "/workspace/payments/converge.yaml",
            __event_call__=confirm,
        )
    )

    assert result["ok"] is True
    tools._request.assert_awaited_once_with(
        "POST",
        "/projects",
        {
            "id": "payments",
            "config_path": "/workspace/payments/converge.yaml",
        },
    )


def test_decision_reads_interrupt_then_confirms_then_posts() -> None:
    tools = _tools_class()()
    calls = []

    async def request(method, path, json_body=None):
        calls.append((method, path, json_body))
        if method == "GET":
            return {
                "ok": True,
                "status_code": 200,
                "data": {
                    "kind": "risk_policy",
                    "risk_flags": ["destructive_migration"],
                },
            }
        return {"ok": True, "status_code": 202, "data": {"status": "running"}}

    confirmations = []

    async def confirm(event):
        confirmations.append(event)
        return True

    tools._request = request
    result = _run(
        tools.decide_run(
            "run-risk",
            "reject",
            __event_call__=confirm,
        )
    )

    assert result["ok"] is True
    assert calls == [
        ("GET", "/runs/run-risk/interrupt", None),
        ("POST", "/runs/run-risk/decision", {"action": "reject"}),
    ]
    message = confirmations[0]["data"]["message"]
    assert "destructive_migration" in message
    assert "Decision: reject" in message


def test_decision_without_confirmation_never_posts_mutation() -> None:
    tools = _tools_class()()
    calls = []

    async def request(method, path, json_body=None):
        calls.append((method, path, json_body))
        return {"ok": True, "status_code": 200, "data": {"kind": "risk_policy"}}

    tools._request = request
    result = _run(tools.decide_run("run-risk", "approve"))

    assert result["cancelled"] is True
    assert calls == [("GET", "/runs/run-risk/interrupt", None)]
