from __future__ import annotations

from pathlib import Path
from typing import Annotated, Any

import typer
from rich.console import Console

from .acceptance import load_supervisor_evidence
from .acceptance_provenance import evaluate_external_acceptance_with_provenance
from .acceptance_supervisor import (
    AcceptanceSupervisorError,
    _validate_acceptance_preconditions,
    supervise_external_acceptance,
)
from .config import load_config, load_run_config_snapshot
from .github import GitHubAdapter, GitHubError
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


def _acceptance_preflight(config_path: Path) -> dict[str, Any]:
    """Fail before model work unless the external release target has authoritative required CI."""

    cfg = load_config(config_path)
    _validate_acceptance_preconditions(cfg)
    try:
        policy = GitHubAdapter(cfg).remote_policy(cfg.base_branch)
    except GitHubError as exc:
        raise AcceptanceSupervisorError(
            f"acceptance remote CI preflight failed: {exc}"
        ) from exc

    if not policy.authoritative:
        raise AcceptanceSupervisorError(
            "acceptance remote CI policy is not authoritative: "
            f"source={policy.source}"
        )
    if not policy.required_checks:
        raise AcceptanceSupervisorError(
            "acceptance base branch must require at least one authoritative GitHub status check; "
            f"source={policy.source}"
        )

    return {
        "target_repository": cfg.github_repo,
        "base_branch": cfg.base_branch,
        "policy_source": policy.source,
        "strict": policy.strict,
        "required_checks": [item.as_dict() for item in policy.required_checks],
    }


def _risk_approval(interrupt: dict[str, Any]) -> str:
    console.print_json(data=interrupt)
    approved = typer.confirm(
        "Approve this deliberately injected acceptance risk and continue without manual code edits?"
    )
    return "approve" if approved else "reject"


@app.command("preflight")
def preflight(
    config: Annotated[
        Path,
        typer.Option("--config", exists=True, readable=True, help="Acceptance project config."),
    ],
) -> None:
    """Verify local acceptance constraints and authoritative required GitHub CI before model work."""

    try:
        result = _acceptance_preflight(config.expanduser().resolve())
    except (AcceptanceSupervisorError, OSError, RuntimeError, ValueError) as exc:
        raise typer.BadParameter(str(exc)) from exc
    console.print_json(data=result)


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

    resolved_config = config.expanduser().resolve()
    try:
        _acceptance_preflight(resolved_config)
        result = supervise_external_acceptance(
            resolved_config,
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
