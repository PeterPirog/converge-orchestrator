from __future__ import annotations

import shutil
import sqlite3
from pathlib import Path
from typing import Annotated

import typer
from langgraph.checkpoint.sqlite import SqliteSaver
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
from .quality import effective_quality_gates
from .spec import compile_contract, is_read_only, sha256_file
from .workflow import build_graph

app = typer.Typer(no_args_is_help=True)
console = Console()

ConfigOption = Annotated[Path, typer.Option("--config", exists=True, readable=True)]
ThreadOption = Annotated[str, typer.Option("--thread-id")]
OfflineOption = Annotated[
    bool,
    typer.Option("--offline", help="Skip live model-gateway connectivity checks."),
]


def _require_executable(binary: str, label: str) -> None:
    if shutil.which(binary) is None:
        raise typer.BadParameter(f"{label} executable not found on PATH: {binary}")


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

    _require_executable(cfg.opencode_binary, "OpenCode")
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
    generated_config = materialize_opencode_config(cfg)

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
    console.print(f"model gateway: {cfg.model_gateway.kind}")
    if gateway_models:
        console.print(f"gateway models visible: {len(gateway_models)}")
    for role, model in agent_models.items():
        console.print(f"agent {role}: {cfg.agents[role].agent} -> {model}")
    console.print(f"generated OpenCode config: {generated_config}")


@app.command()
def run(
    config: ConfigOption,
    thread_id: ThreadOption = "default",
) -> None:
    """Run the autonomous convergence loop until a terminal state or interrupt."""
    cfg = load_config(config)
    db = sqlite3.connect(cfg.state_dir / "langgraph.sqlite", check_same_thread=False)
    graph = build_graph(checkpointer=SqliteSaver(db))
    graph_config = {"configurable": {"thread_id": thread_id}}
    result = graph.invoke(
        {"config_path": str(config.resolve()), "thread_id": thread_id},
        config=graph_config,
    )
    console.print_json(data=result)


if __name__ == "__main__":
    app()
