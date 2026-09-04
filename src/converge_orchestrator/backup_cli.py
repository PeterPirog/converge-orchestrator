from __future__ import annotations

from pathlib import Path
from typing import Annotated

import typer
from rich.console import Console

from .backup import BackupError, create_deployment_backup, verify_deployment_backup
from .persistence import (
    PersistenceBackend,
    configured_control_db_path,
    configured_database_url,
)
from .restore import RestoreError, plan_deployment_restore
from .restore_apply import RestoreApplyError, apply_sqlite_restore

app = typer.Typer(no_args_is_help=True)
console = Console()

BackupPath = Annotated[Path, typer.Argument(help="Backup directory path.")]
ConfirmationToken = Annotated[
    str,
    typer.Option(
        "--confirmation-token",
        help="Exact token emitted by a ready restore-plan for the current targets.",
    ),
]


def _summary(status: str, root: Path, manifest) -> dict[str, object]:
    return {
        "status": status,
        "backup": str(root.expanduser().resolve()),
        "created_at": manifest.created_at,
        "persistence_backend": manifest.persistence_backend,
        "projects": len(manifest.projects),
        "files": len(manifest.files),
    }


@app.command("create")
def create_backup(destination: BackupPath) -> None:
    """Create one fail-closed, globally quiescent deployment backup."""
    backend = PersistenceBackend(configured_control_db_path())
    try:
        manifest = create_deployment_backup(
            registry=backend.registry,
            persistence_backend=backend.kind,
            control_db_path=backend.control_db_path,
            database_url=backend.database_url,
            destination=destination,
        )
    except (BackupError, RuntimeError, ValueError) as exc:
        raise typer.BadParameter(str(exc)) from exc
    console.print_json(data=_summary("created", destination, manifest))


@app.command("verify")
def verify_backup(root: BackupPath) -> None:
    """Verify manifest, file set, sizes and SHA-256 hashes without restoring data."""
    try:
        manifest = verify_deployment_backup(root)
    except (BackupError, RuntimeError, ValueError) as exc:
        raise typer.BadParameter(str(exc)) from exc
    console.print_json(data=_summary("verified", root, manifest))


@app.command("restore-plan")
def restore_plan(root: BackupPath) -> None:
    """Verify restore targets and emit a confirmation-bound, read-only plan."""
    try:
        plan = plan_deployment_restore(
            root,
            control_db_path=configured_control_db_path(),
            database_url=configured_database_url(),
        )
    except (RestoreError, RuntimeError, ValueError) as exc:
        raise typer.BadParameter(str(exc)) from exc
    console.print_json(data=plan.model_dump(mode="json"))
    if not plan.ready:
        raise typer.Exit(code=2)


@app.command("restore-apply")
def restore_apply(root: BackupPath, confirmation_token: ConfirmationToken) -> None:
    """Apply or resume an explicitly approved SQLite deployment restore."""
    try:
        result = apply_sqlite_restore(
            root,
            confirmation_token=confirmation_token,
            control_db_path=configured_control_db_path(),
            database_url=configured_database_url(),
        )
    except (BackupError, RestoreApplyError, RestoreError, RuntimeError, ValueError) as exc:
        raise typer.BadParameter(str(exc)) from exc
    console.print_json(data=result.model_dump(mode="json"))


if __name__ == "__main__":
    app()
