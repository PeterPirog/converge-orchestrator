"""
title: Converge Operator
author: PeterPirog
description: Operator bridge from Open WebUI to the Converge FastAPI/LangGraph control plane.
required_open_webui_version: 0.10.0
requirements: httpx
version: 0.1.0
license: MIT
"""

from __future__ import annotations

import json
from typing import Any, Literal

import httpx
from pydantic import BaseModel, Field


class Tools:
    """Operate Converge without moving durable workflow state out of LangGraph."""

    class Valves(BaseModel):
        base_url: str = Field(
            default="http://host.docker.internal:8088",
            description="Base URL of the Converge control API.",
        )
        api_token: str = Field(
            default="",
            description="Bearer token matching CONVERGE_API_TOKEN.",
            json_schema_extra={"input": {"type": "password"}},
        )
        timeout_seconds: float = Field(
            default=30.0,
            ge=1.0,
            le=300.0,
            description="HTTP timeout for one control-plane request.",
        )

    def __init__(self):
        self.valves = self.Valves()

    def _headers(self) -> dict[str, str]:
        token = self.valves.api_token.strip()
        return {"Authorization": f"Bearer {token}"} if token else {}

    def _url(self, path: str) -> str:
        return f"{self.valves.base_url.rstrip('/')}/{path.lstrip('/')}"

    async def _request(
        self,
        method: str,
        path: str,
        json_body: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        try:
            async with httpx.AsyncClient(timeout=self.valves.timeout_seconds) as client:
                response = await client.request(
                    method,
                    self._url(path),
                    headers=self._headers(),
                    json=json_body,
                )
        except httpx.RequestError as exc:
            return {
                "ok": False,
                "error": f"Converge API request failed: {type(exc).__name__}",
            }

        try:
            payload: Any = response.json()
        except ValueError:
            payload = response.text[:4000]

        if response.is_error:
            detail = payload.get("detail") if isinstance(payload, dict) else payload
            return {
                "ok": False,
                "status_code": response.status_code,
                "error": str(detail)[:4000],
            }
        return {
            "ok": True,
            "status_code": response.status_code,
            "data": payload,
        }

    @staticmethod
    async def _confirmed(
        __event_call__: Any,
        title: str,
        message: str,
    ) -> tuple[bool, str | None]:
        if __event_call__ is None:
            return False, "Open WebUI interactive confirmation is unavailable."
        result = await __event_call__(
            {
                "type": "confirmation",
                "data": {"title": title, "message": message[:6000]},
            }
        )
        if result is True:
            return True, None
        if isinstance(result, dict):
            if result.get("error"):
                return False, str(result["error"])[:1000]
            if result.get("confirmed") is True:
                return True, None
        return False, "Operator cancelled or did not explicitly confirm the operation."

    @staticmethod
    def _cancelled(reason: str | None = None) -> dict[str, Any]:
        return {
            "ok": False,
            "cancelled": True,
            "error": reason
            or "Explicit Open WebUI confirmation is required for mutating Converge operations.",
        }

    async def list_projects(self) -> dict[str, Any]:
        """List projects registered in the Converge control plane. This is read-only."""
        return await self._request("GET", "/projects")

    async def run_status(self, run_id: str) -> dict[str, Any]:
        """Read durable LangGraph run status, current task, gates, review and CI state."""
        return await self._request("GET", f"/runs/{run_id}")

    async def project_compliance(self, project_id: str) -> dict[str, Any]:
        """Read the persisted compliance matrix for a registered project."""
        return await self._request("GET", f"/projects/{project_id}/compliance")

    async def task_evidence(self, task_id: str) -> dict[str, Any]:
        """Read auditable evidence artifacts for one Converge task."""
        return await self._request("GET", f"/tasks/{task_id}/evidence")

    async def pending_interrupt(self, run_id: str) -> dict[str, Any]:
        """Read the pending LangGraph/HITL interrupt for a run, if one exists."""
        return await self._request("GET", f"/runs/{run_id}/interrupt")

    async def register_project(
        self,
        project_id: str,
        config_path: str,
        __event_call__: Any = None,
    ) -> dict[str, Any]:
        """Register a converge.yaml project after explicit operator confirmation."""
        confirmed, reason = await self._confirmed(
            __event_call__,
            "Register Converge project?",
            f"Register project '{project_id}' using configuration:\n{config_path}",
        )
        if not confirmed:
            return self._cancelled(reason)
        return await self._request(
            "POST",
            "/projects",
            {"id": project_id, "config_path": config_path},
        )

    async def bootstrap_project(
        self,
        project_id: str,
        __event_call__: Any = None,
    ) -> dict[str, Any]:
        """Compile/pin immutable requirements and initialize compliance after confirmation."""
        confirmed, reason = await self._confirmed(
            __event_call__,
            "Bootstrap Converge project?",
            (
                f"Bootstrap '{project_id}'. Converge will read and pin the immutable Source of "
                "Truth and initialize durable compliance state."
            ),
        )
        if not confirmed:
            return self._cancelled(reason)
        return await self._request("POST", f"/projects/{project_id}/bootstrap")

    async def start_project(
        self,
        project_id: str,
        __event_call__: Any = None,
    ) -> dict[str, Any]:
        """Start an autonomous LangGraph convergence run after explicit confirmation."""
        confirmed, reason = await self._confirmed(
            __event_call__,
            "Start autonomous Converge run?",
            (
                f"Start project '{project_id}'. The durable LangGraph workflow may create a "
                "worktree, modify code, run tests and open a pull request according to project "
                "policy."
            ),
        )
        if not confirmed:
            return self._cancelled(reason)
        return await self._request("POST", f"/projects/{project_id}/run")

    async def pause_run(
        self,
        run_id: str,
        __event_call__: Any = None,
    ) -> dict[str, Any]:
        """Request a cooperative pause at the next safe LangGraph boundary."""
        confirmed, reason = await self._confirmed(
            __event_call__,
            "Pause Converge run?",
            f"Request a cooperative pause for run '{run_id}' at its next safe boundary.",
        )
        if not confirmed:
            return self._cancelled(reason)
        return await self._request("POST", f"/runs/{run_id}/pause")

    async def resume_run(
        self,
        run_id: str,
        __event_call__: Any = None,
    ) -> dict[str, Any]:
        """Resume a cooperatively paused LangGraph run after confirmation."""
        confirmed, reason = await self._confirmed(
            __event_call__,
            "Resume Converge run?",
            f"Resume run '{run_id}' from its durable LangGraph checkpoint.",
        )
        if not confirmed:
            return self._cancelled(reason)
        return await self._request("POST", f"/runs/{run_id}/resume")

    async def decide_run(
        self,
        run_id: str,
        action: Literal["approve", "edit", "reject", "retry", "stop"],
        task: dict[str, Any] | None = None,
        __event_call__: Any = None,
    ) -> dict[str, Any]:
        """Resolve a pending HITL interrupt after showing its evidence and confirming the action."""
        interrupt = await self._request("GET", f"/runs/{run_id}/interrupt")
        if not interrupt.get("ok"):
            return interrupt

        message = (
            f"Run: {run_id}\n"
            f"Decision: {action}\n\n"
            "Pending interrupt:\n"
            f"{json.dumps(interrupt.get('data'), ensure_ascii=False, indent=2)[:3500]}"
        )
        if task is not None:
            message += (
                "\n\nReplacement/edited task:\n"
                + json.dumps(task, ensure_ascii=False, indent=2)[:1800]
            )
        confirmed, reason = await self._confirmed(
            __event_call__,
            "Apply Converge HITL decision?",
            message,
        )
        if not confirmed:
            return self._cancelled(reason)

        body: dict[str, Any] = {"action": action}
        if task is not None:
            body["task"] = task
        return await self._request("POST", f"/runs/{run_id}/decision", body)
