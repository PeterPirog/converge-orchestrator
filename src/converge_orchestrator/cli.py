from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Annotated

import typer
from langgraph.checkpoint.sqlite import SqliteSaver
from rich.console import Console

from .config import load_config
from .spec import compile_contract, is_read_only, sha256_file
from .workflow import build_graph

app = typer.Typer(no_args_is_help=True)
console = Console()

ConfigOption = Annotated[Path, typer.Option("--config", exists=True, readable=True)]
ThreadOption = Annotated[str, typer.Option("--thread-id")]


@app.command()
def doctor(config: ConfigOption) -> None:
    """Validate paths and show the immutable specification fingerprint."""
    cfg = load_config(config)
    contract = compile_contract(cfg.requirements_path)
    console.print(f"repo: {cfg.repo_path}")
    console.print(f"requirements: {cfg.requirements_path}")
    console.print(f"requirements read-only: {is_read_only(cfg.requirements_path)}")
    console.print(f"sha256: {sha256_file(cfg.requirements_path)}")
    console.print(f"compiled requirements: {len(contract.requirements)}")
    console.print(f"github: {cfg.github_repo or 'disabled'}")


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
