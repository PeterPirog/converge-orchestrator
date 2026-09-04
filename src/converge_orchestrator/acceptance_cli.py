from __future__ import annotations

from pathlib import Path
from typing import Annotated

import typer
from rich.console import Console

from .acceptance import evaluate_external_acceptance, load_supervisor_evidence
from .config import load_run_config_snapshot
from .persistence import configured_control_db_path
from .runtime_service import ScheduledRunController

app = typer.Typer(no_args_is_help=True)
console = Console()

RunIdOption = Annotated[str, typer.Option("--run-id", help="Durable Converge run ID to evaluate.")]
SupervisorOption = Annotated[
    Path | None,
    typer.Option(
        "--supervisor-evidence",
        exists=True,
        readable=True,
        help=(
            "JSON evidence emitted by the external acceptance supervisor for process restart, "
            "deliberately exceptional HITL and final independent audit."
        ),
    ),
]


@app.command("report")
def report(
    run_id: RunIdOption,
    supervisor_evidence: SupervisorOption = None,
) -> None:
    """Fail closed unless one external acceptance run satisfies every release-gate check."""

    controller = ScheduledRunController(
        configured_control_db_path(),
        restore_on_start=False,
    )
    try:
        record = controller.registry.get_run(run_id)
        snapshot_path = record.get("config_snapshot_path")
        snapshot_sha256 = record.get("config_snapshot_sha256")
        if not snapshot_path or not snapshot_sha256:
            raise ValueError(
                "external acceptance requires a run with hash-pinned configuration metadata"
            )
        cfg = load_run_config_snapshot(str(snapshot_path), str(snapshot_sha256))
        status = controller.status(run_id)
        supervisor = load_supervisor_evidence(supervisor_evidence)
        result = evaluate_external_acceptance(
            cfg,
            status,
            supervisor_evidence=supervisor,
        )
    except (KeyError, OSError, RuntimeError, ValueError) as exc:
        raise typer.BadParameter(str(exc)) from exc

    console.print_json(data=result.model_dump(mode="json"))
    if not result.ready:
        raise typer.Exit(code=1)


if __name__ == "__main__":
    app()
