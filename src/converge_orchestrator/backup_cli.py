from __future__ import annotations

from pathlib import Path
from typing import Annotated

import typer
from rich.console import Console

from .backup import BackupError, create_deployment_backup, verify_deployment_backup
from .persistence import PersistenceBackend, configured_control_db_path

app = typer.Typer(no_args_is_help=True)
console = Console()

BackupPath = Annotated[Path, typer.Argument(help="Backup directory path.")]


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


if __name__ == "__main__":
    app()
