from __future__ import annotations

from pathlib import Path
from typing import Annotated, Any

import typer
from rich.console import Console

from .acceptance import load_supervisor_evidence
from .acceptance_provenance import evaluate_external_acceptance_with_provenance
from .acceptance_supervisor import AcceptanceSupervisorError, supervise_external_acceptance
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


def _risk_approval(interrupt: dict[str, Any]) -> str:
    console.print_json(data=interrupt)
    approved = typer.confirm(
        "Approve this deliberately injected acceptance risk and continue without manual code edits?"
    )
    return "approve" if approved else "reject"


@app.command("supervise")
def supervise(
    config: Annotated[
        Path,
        typer.Option("--config", exists=True, readable=True, help="Acceptance project config."),
    ],
    project_id: Annotated[
        str,
        typer.Option(
            "--project-id",
            help="Dedicated control-plane project ID for the acceptance run.",
        ),
    ],
    expected_risk_flag: Annotated[
        str,
        typer.Option(
            "--expected-risk-flag",
            help=(
                "Predeclared deterministic risk flag intentionally exercised by the "
                "acceptance target."
            ),
        ),
    ],
    output: Annotated[
        Path,
        typer.Option(
            "--output",
            help=(
                "Supervisor evidence JSON written by the command, not hand-authored "
                "after the run."
            ),
        ),
    ] = Path("acceptance-supervisor.json"),
    poll_seconds: Annotated[
        float,
        typer.Option("--poll-seconds", min=0.1, help="Read-only supervisor polling cadence."),
    ] = 1.0,
) -> None:
    """Run the live external-repository release scenario through the normal API controller."""

    try:
        result = supervise_external_acceptance(
            config,
            project_id=project_id,
            expected_risk_flag=expected_risk_flag,
            output_path=output.expanduser().resolve(),
            decision_provider=_risk_approval,
            poll_seconds=poll_seconds,
        )
    except (AcceptanceSupervisorError, OSError, RuntimeError, ValueError) as exc:
        raise typer.BadParameter(str(exc)) from exc

    console.print_json(data=result.model_dump(mode="json"))
    if not result.ready:
        raise typer.Exit(code=1)


@app.command("report")
def report(
    run_id: RunIdOption,
    supervisor_evidence: SupervisorOption = None,
) -> None:
    """Fail closed unless live supervisor artifacts satisfy every release-gate check."""

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
        result = evaluate_external_acceptance_with_provenance(
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
