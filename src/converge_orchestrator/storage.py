from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any

from langgraph.checkpoint.sqlite import SqliteSaver

from .registry import ControlRegistry


class PostgresSupportError(RuntimeError):
    """Raised when PostgreSQL runtime state is requested without its optional dependencies."""


def _postgres_components():
    try:
        from langgraph.checkpoint.postgres import PostgresSaver
        from psycopg import Connection
        from psycopg.rows import dict_row
    except ImportError as exc:  # pragma: no cover - covered by default-install behavior
        raise PostgresSupportError(
            "PostgreSQL runtime state requires the optional 'postgres' extra; "
            "install converge-orchestrator[postgres]"
        ) from exc
    return PostgresSaver, Connection, dict_row


def create_control_registry(registry_path: Path, postgres_dsn: str | None = None) -> Any:
    """Create the control registry without importing optional PostgreSQL code for SQLite users."""
    if not postgres_dsn:
        return ControlRegistry(registry_path)
    try:
        from .registry_postgres import PostgresControlRegistry
    except ImportError as exc:  # pragma: no cover - covered by default-install behavior
        raise PostgresSupportError(
            "PostgreSQL runtime state requires the optional 'postgres' extra; "
            "install converge-orchestrator[postgres]"
        ) from exc
    return PostgresControlRegistry(postgres_dsn)


def setup_checkpoint_storage(postgres_dsn: str | None = None) -> None:
    """Run LangGraph PostgreSQL migrations once at controller/process startup."""
    if not postgres_dsn:
        return
    PostgresSaver, Connection, dict_row = _postgres_components()
    connection = Connection.connect(
        postgres_dsn,
        autocommit=True,
        prepare_threshold=0,
        row_factory=dict_row,
    )
    try:
        saver = PostgresSaver(connection)
        saver.setup()
    finally:
        connection.close()


def open_checkpointer(
    state_dir: Path,
    postgres_dsn: str | None = None,
) -> tuple[Any, Any]:
    """Open one checkpointer resource; caller owns and must close the returned connection."""
    if postgres_dsn:
        PostgresSaver, Connection, dict_row = _postgres_components()
        connection = Connection.connect(
            postgres_dsn,
            autocommit=True,
            prepare_threshold=0,
            row_factory=dict_row,
        )
        return PostgresSaver(connection), connection

    state_dir.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(
        state_dir / "langgraph.sqlite",
        check_same_thread=False,
    )
    return SqliteSaver(connection), connection


def is_database_error(exc: Exception) -> bool:
    """Recognize backend database failures without making PostgreSQL a mandatory dependency."""
    if isinstance(exc, sqlite3.Error):
        return True
    try:
        from psycopg import Error as PsycopgError
    except ImportError:
        return False
    return isinstance(exc, PsycopgError)


def is_transient_checkpoint_error(exc: Exception) -> bool:
    """Return whether checkpoint inspection can safely be retried without changing graph state."""
    if isinstance(exc, sqlite3.OperationalError):
        message = str(exc).lower()
        return "locked" in message or "busy" in message
    try:
        from psycopg import OperationalError as PsycopgOperationalError
    except ImportError:
        return False
    return isinstance(exc, PsycopgOperationalError)
