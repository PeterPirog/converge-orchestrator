import sqlite3
from pathlib import Path

import typer
from langgraph.checkpoint.sqlite import SqliteSaver
from rich.console import Console

from .config import load_config
from .spec import compile_contract, sha256_file
from .workflow import build_graph

app = typer.Typer(no_args_is_help=True)
console = Console()


@app.command()
def doctor(config: Path = typer.Option(..., exists=True, readable=True)) -> None:
    """Validate paths and show the immutable specification fingerprint."""
    cfg = load_config(config)
    console.print(f"repo: {cfg.repo_path}")
    console.print(f"requirements: {cfg.requirements_path}")
    console.print(f"sha256: {sha256_file(cfg.requirements_path)}")
    console.print(f"compiled requirements: {len(compile_contract(cfg.requirements_path))}")


@app.command()
def run(config: Path = typer.Option(..., exists=True, readable=True), thread_id: str = typer.Option("default")) -> None:
    """Run one autonomous convergence iteration."""
    cfg = load_config(config)
    db = sqlite3.connect(cfg.state_dir / "langgraph.sqlite", check_same_thread=False)
    graph = build_graph(checkpointer=SqliteSaver(db))
    result = graph.invoke({"config_path": str(config.resolve())}, config={"configurable": {"thread_id": thread_id}})
    console.print_json(data=result)


if __name__ == "__main__":
    app()
