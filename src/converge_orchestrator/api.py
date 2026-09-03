from __future__ import annotations

import os
import secrets
from pathlib import Path
from typing import Any, Literal

import uvicorn
from fastapi import FastAPI, HTTPException, Request, status
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from .runtime_service import ScheduledRunController


class ProjectRegistration(BaseModel):
    id: str = Field(min_length=1, pattern=r"^[A-Za-z0-9_.-]+$")
    config_path: Path


class DecisionRequest(BaseModel):
    action: Literal["approve", "edit", "reject", "retry", "stop"]
    task: dict[str, Any] | None = None


def _run_payload(raw: dict[str, Any]) -> dict[str, Any]:
    payload = dict(raw)
    values = payload.pop("values", {}) or {}
    payload["state"] = {
        "status": values.get("status"),
        "iteration": values.get("iteration", 0),
        "task": values.get("task"),
        "compliance": values.get("compliance"),
        "quality_results": values.get("quality_results", []),
        "review_result": values.get("review_result"),
        "pr": values.get("pr"),
        "ci": values.get("ci"),
        "risk_flags": values.get("risk_flags", []),
        "message": values.get("message"),
    }
    return payload


def create_app(
    registry_path: Path | None = None,
    api_token: str | None = None,
) -> FastAPI:
    path = registry_path or Path(
        os.environ.get("CONVERGE_CONTROL_DB", ".converge/control.sqlite")
    )
    controller = ScheduledRunController(path)
    token = api_token if api_token is not None else os.environ.get("CONVERGE_API_TOKEN")
    app = FastAPI(
        title="Converge Orchestrator API",
        version="0.3.0",
        description="Durable control plane for requirements-driven autonomous code convergence.",
    )
    app.state.controller = controller

    if token:

        @app.middleware("http")
        async def require_bearer_token(request: Request, call_next):
            if request.url.path == "/health":
                return await call_next(request)
            authorization = request.headers.get("Authorization", "")
            scheme, separator, supplied = authorization.partition(" ")
            valid = (
                separator == " "
                and scheme.lower() == "bearer"
                and bool(supplied)
                and secrets.compare_digest(supplied, token)
            )
            if not valid:
                return JSONResponse(
                    status_code=status.HTTP_401_UNAUTHORIZED,
                    content={"detail": "Unauthorized"},
                    headers={"WWW-Authenticate": "Bearer"},
                )
            return await call_next(request)

    @app.get("/health")
    def health() -> dict[str, str]:
        return {"status": "ok"}

    @app.post("/projects", status_code=status.HTTP_201_CREATED)
    def register_project(request: ProjectRegistration) -> dict[str, Any]:
        try:
            return controller.register_project(request.id, request.config_path)
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc

    @app.get("/projects")
    def list_projects() -> list[dict[str, Any]]:
        return controller.registry.list_projects()

    @app.post("/projects/{project_id}/bootstrap")
    def bootstrap_project(project_id: str) -> dict[str, Any]:
        try:
            state = controller.bootstrap_project(project_id)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="Project not found") from exc
        except (RuntimeError, ValueError) as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        compliance = state["compliance"]
        entries = compliance.get("entries", {})
        counts: dict[str, int] = {}
        for entry in entries.values():
            entry_status = str(entry["status"])
            counts[entry_status] = counts.get(entry_status, 0) + 1
        return {
            "project_id": project_id,
            "requirements_hash": state["requirements_hash"],
            "requirements": len(state["requirements"]),
            "compliance": counts,
        }

    @app.post(
        "/projects/{project_id}/run",
        status_code=status.HTTP_202_ACCEPTED,
    )
    def start_run(project_id: str) -> dict[str, Any]:
        try:
            return controller.start_run(project_id)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="Project not found") from exc
        except RuntimeError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc

    @app.get("/runs/{run_id}")
    def get_run(run_id: str) -> dict[str, Any]:
        try:
            return _run_payload(controller.status(run_id))
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="Run not found") from exc

    @app.post("/runs/{run_id}/pause", status_code=status.HTTP_202_ACCEPTED)
    def pause_run(run_id: str) -> dict[str, Any]:
        try:
            return _run_payload(controller.request_pause(run_id))
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="Run not found") from exc
        except RuntimeError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc

    @app.post("/runs/{run_id}/resume", status_code=status.HTTP_202_ACCEPTED)
    def resume_run(run_id: str) -> dict[str, Any]:
        try:
            return controller.resume(run_id)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="Run not found") from exc
        except RuntimeError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc

    @app.get("/runs/{run_id}/interrupt")
    def get_interrupt(run_id: str) -> dict[str, Any]:
        try:
            payload = controller.interrupt(run_id)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="Run not found") from exc
        if payload is None:
            raise HTTPException(status_code=404, detail="Run has no pending interrupt")
        return payload

    @app.post("/runs/{run_id}/decision", status_code=status.HTTP_202_ACCEPTED)
    def decide(run_id: str, request: DecisionRequest) -> dict[str, Any]:
        try:
            return controller.decide(run_id, request.model_dump(exclude_none=True))
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="Run not found") from exc
        except RuntimeError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc

    @app.get("/projects/{project_id}/compliance")
    def project_compliance(project_id: str) -> dict[str, Any]:
        try:
            return controller.compliance(project_id)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="Project not found") from exc
        except FileNotFoundError as exc:
            raise HTTPException(
                status_code=409,
                detail="Project has not been bootstrapped",
            ) from exc

    @app.get("/tasks/{task_id}/evidence")
    def task_evidence(task_id: str) -> dict[str, Any]:
        matches = controller.evidence(task_id)
        if not matches:
            raise HTTPException(status_code=404, detail="Evidence not found")
        return {"task_id": task_id, "matches": matches}

    return app


app = create_app()


def main() -> None:
    host = os.environ.get("CONVERGE_API_HOST", "127.0.0.1")
    port = int(os.environ.get("CONVERGE_API_PORT", "8088"))
    uvicorn.run("converge_orchestrator.api:app", host=host, port=port, reload=False)
