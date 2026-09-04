from __future__ import annotations

import re
import shutil
import time
from pathlib import Path
from typing import Annotated, Any

import typer
from rich.console import Console

from .config import load_config
from .inspector import inspect_repository
from .model_gateway import (
    ModelGatewayError,
    configured_gateway_model_ids,
    gateway_model_ids,
)
from .opencode_config import (
    materialize_opencode_config,
    resolve_agent_model,
    resolve_agent_variant,
)
from .persistence import (
    configured_control_db_path,
    configured_database_url,
    setup_postgres,
)
from .quality import effective_quality_gates
from .runtime_service import ScheduledRunController
from .sandbox import ExecutionSandbox, SandboxPreflightError
from .spec import compile_contract, is_read_only, sha256_file

app = typer.Typer(no_args_is_help=True)
console = Console()

ConfigOption = Annotated[Path, typer.Option("--config", exists=True, readable=True)]
ThreadOption = Annotated[
    str | None,
    typer.Option(
        "--thread-id",
        help="Optional unique LangGraph thread ID; generated automatically when omitted.",
    ),
]
ProjectIdOption = Annotated[
    str | None,
    typer.Option(
        "--project-id",
        help="Control-plane project ID; defaults to project.name or the config filename stem.",
    ),
]
OfflineOption = Annotated[
    bool,
    typer.Option("--offline", help="Skip live model-gateway connectivity checks."),
]

_PROJECT_ID_PATTERN = re.compile(r"^[A-Za-z0-9_.-]+$")
_CLI_STATUS_POLL_SECONDS = 1.0
_CLI_RECONCILE_SECONDS = 5.0


def _require_executable(binary: str, label: str) -> None:
    if shutil.which(binary) is None:
        raise typer.BadParameter(f"{label} executable not found on PATH: {binary}")


def _resolve_cli_project_id(config: Path, cfg: Any, explicit: str | None) -> str:
    project_id = explicit or cfg.project_name or config.stem
    if not project_id or not _PROJECT_ID_PATTERN.fullmatch(project_id):
        raise typer.BadParameter(
            "CLI project ID must match [A-Za-z0-9_.-]+; set project.name or --project-id"
        )
    return project_id


def _select_or_start_run(
    controller: ScheduledRunController,
    project_id: str,
    thread_id: str | None,
) -> dict[str, Any]:
    history = controller.registry.runs_for_project(project_id)
    unfinished = [record for record in history if not record.get("finished_at")]
    if len(unfinished) > 1:
        raise RuntimeError(
            f"Project {project_id} has multiple unfinished runs; refusing ambiguous CLI recovery"
        )
    if unfinished:
        record = unfinished[0]
        if thread_id is not None and record.get("thread_id") != thread_id:
            raise RuntimeError(
                f"Project {project_id} already has active run {record['id']} on thread "
                f"{record.get('thread_id')}; omit --thread-id or use that active thread"
            )
        return record

    if thread_id is not None:
        for record in history:
            if record.get("thread_id") == thread_id:
                raise RuntimeError(
                    f"Thread {thread_id} already belongs to finished run {record['id']}; "
                    "omit --thread-id or choose a new unique thread ID"
                )
    return controller.start_run(project_id, thread_id=thread_id)


def _wait_until_terminal_or_human_interrupt(
    controller: ScheduledRunController,
    run_id: str,
    poll_seconds: float = _CLI_STATUS_POLL_SECONDS,
    reconcile_seconds: float = _CLI_RECONCILE_SECONDS,
) -> dict[str, Any]:
    if poll_seconds <= 0:
        raise ValueError("poll_seconds must be positive")
    if reconcile_seconds <= 0:
        raise ValueError("reconcile_seconds must be positive")

    next_reconcile = time.monotonic() + reconcile_seconds
    while True:
        record = controller.registry.get_run(run_id)
        if record.get("finished_at"):
            return controller.status(run_id)

        if record.get("status") in {"paused", "interrupted"}:
            result = controller.status(run_id)
            interrupt_payload = result.get("interrupt")
            if interrupt_payload and interrupt_payload.get("kind") != "ci_wait":
                return result
            next_reconcile = time.monotonic() + reconcile_seconds
        elif time.monotonic() >= next_reconcile:
            # A synchronous CLI process can outlive an in-memory recovery timer. Reconcile the
            # durable checkpoint periodically so recoverable/CI states self-heal without HITL.
            result = controller.status(run_id)
            if result.get("finished_at"):
                return result
            interrupt_payload = result.get("interrupt")
            if interrupt_payload and interrupt_payload.get("kind") != "ci_wait":
                return result
            next_reconcile = time.monotonic() + reconcile_seconds

        time.sleep(poll_seconds)


@app.command("models")
def list_models(config: ConfigOption) -> None:
    """List model IDs visible through the configured OpenWebUI/OpenAI-compatible gateway."""
    cfg = load_config(config)
    if cfg.model_gateway.kind == "existing":
        console.print("model gateway: existing OpenCode providers")
        console.print("Run `opencode models` to inspect models from native OpenCode providers.")
        return
    try:
        visible = sorted(gateway_model_ids(cfg))
    except ModelGatewayError as exc:
        raise typer.BadParameter(str(exc)) from exc
    if not visible:
        console.print("No models returned by the configured gateway.")
        return
    console.print(f"gateway: {cfg.model_gateway.base_url}")
    for model_id in visible:
        console.print(model_id)


@app.command("persistence-setup")
def persistence_setup() -> None:
    """Initialize the shared PostgreSQL control and LangGraph checkpoint schema."""
    if not configured_database_url():
        raise typer.BadParameter(
            "CONVERGE_DATABASE_URL is not configured; SQLite needs no setup command"
        )
    try:
        setup_postgres()
    except (RuntimeError, ValueError) as exc:
        raise typer.BadParameter(str(exc)) from exc
    console.print("PostgreSQL persistence schema is ready.")


@app.command()
def doctor(config: ConfigOption, offline: OfflineOption = False) -> None:
    """Validate one project config before the autonomous run starts."""
    cfg = load_config(config)
    if not cfg.repo_path.is_dir():
        raise typer.BadParameter(f"repository directory does not exist: {cfg.repo_path}")
    if not cfg.requirements_path.is_file():
        raise typer.BadParameter(
            f"architecture requirements file does not exist: {cfg.requirements_path}"
        )
    if cfg.require_spec_read_only and not is_read_only(cfg.requirements_path):
        raise typer.BadParameter(
            f"architecture requirements must be read-only: {cfg.requirements_path}"
        )

    if cfg.sandbox.mode == "host":
        _require_executable(cfg.opencode_binary, "OpenCode")
    else:
        try:
            ExecutionSandbox(cfg).preflight()
        except SandboxPreflightError as exc:
            raise typer.BadParameter(str(exc)) from exc
    if cfg.github_repo:
        _require_executable(cfg.github_binary, "GitHub CLI")

    contract = compile_contract(cfg.requirements_path)
    requirement_ids = {item.id for item in contract.requirements}
    unknown_verifiers = set(cfg.requirement_verifiers) - requirement_ids
    if unknown_verifiers:
        raise typer.BadParameter(
            f"requirement_verifiers reference unknown IDs: {sorted(unknown_verifiers)}"
        )

    profile = inspect_repository(cfg.repo_path)
    gates = effective_quality_gates(cfg, cfg.repo_path)
    try:
        generated_config = materialize_opencode_config(cfg)
    except ValueError as exc:
        raise typer.BadParameter(str(exc)) from exc

    agent_models: dict[str, str] = {}
    for role, agent in cfg.agents.items():
        try:
            model = resolve_agent_model(cfg, agent) or "OpenCode default"
        except ValueError as exc:
            raise typer.BadParameter(f"invalid model for agent {role}: {exc}") from exc
        variant = resolve_agent_variant(cfg, agent)
        agent_models[role] = f"{model}#{variant}" if variant else model

    gateway_models: set[str] = set()
    if cfg.model_gateway.kind != "existing" and not offline:
        try:
            gateway_models = gateway_model_ids(cfg)
        except ModelGatewayError as exc:
            raise typer.BadParameter(str(exc)) from exc
        missing_models = configured_gateway_model_ids(cfg) - gateway_models
        if missing_models:
            raise typer.BadParameter(
                f"configured models are not visible in the gateway: {sorted(missing_models)}"
            )

    console.print(f"configuration version: {cfg.version}")
    console.print(f"project: {cfg.project_name or config.stem}")
    console.print(f"repo: {cfg.repo_path}")
    console.print(f"requirements: {cfg.requirements_path}")
    console.print(f"requirements read-only: {is_read_only(cfg.requirements_path)}")
    console.print(f"sha256: {sha256_file(cfg.requirements_path)}")
    console.print(f"compiled requirements: {len(contract.requirements)}")
    console.print(f"detected stacks: {', '.join(profile.stacks) or 'none'}")
    console.print(f"quality gates: {', '.join(gate.name for gate in gates) or 'none'}")
    console.print(f"deterministic requirement verifiers: {len(cfg.requirement_verifiers)}")
    console.print(f"github: {cfg.github_repo or 'disabled'}")
    console.print(f"sandbox: {cfg.sandbox.mode}")
    if cfg.sandbox.mode == "container":
        console.print(f"sandbox image: {cfg.sandbox.image}")
        console.print(f"agent network: {cfg.sandbox.agent_network}")
        console.print(f"quality network: {cfg.sandbox.quality_network}")
    console.print(f"OpenCode binary: {cfg.opencode_binary}")
    console.print(f"model gateway: {cfg.model_gateway.kind}")
    console.print(
        "persistence: "
        + ("PostgreSQL (shared)" if configured_database_url() else "SQLite (local)")
    )
    if gateway_models:
        console.print(f"gateway models visible: {len(gateway_models)}")
    for role, model in agent_models.items():
        console.print(f"agent {role}: {cfg.agents[role].agent} -> {model}")
    console.print(f"generated OpenCode config: {generated_config}")


@app.command()
def run(
    config: ConfigOption,
    thread_id: ThreadOption = None,
    project_id: ProjectIdOption = None,
) -> None:
    """Run or recover one durable project run until terminal state or human interrupt."""
    resolved_config = config.expanduser().resolve()
    cfg = load_config(resolved_config)
    resolved_project_id = _resolve_cli_project_id(
        resolved_config,
        cfg,
        project_id,
    )
    try:
        controller = ScheduledRunController(
            configured_control_db_path(),
            restore_on_start=False,
        )
        controller.register_project(resolved_project_id, resolved_config)
        controller.restore_durable_runs(resolved_project_id)
        record = _select_or_start_run(controller, resolved_project_id, thread_id)
        result = _wait_until_terminal_or_human_interrupt(controller, str(record["id"]))
    except (RuntimeError, ValueError) as exc:
        raise typer.BadParameter(str(exc)) from exc
    console.print_json(data=result)


if __name__ == "__main__":
    app()
