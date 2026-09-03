from __future__ import annotations

import os
import sqlite3
from pathlib import Path
from typing import Any

from langgraph.checkpoint.sqlite import SqliteSaver

from .registry import ControlRegistry
from .registry_postgres import PostgresControlRegistry

_DATABASE_URL_ENV = "CONVERGE_DATABASE_URL"
_STRICT_MSGPACK_ENV = "LANGGRAPH_STRICT_MSGPACK"
_SCHEMA_PROBE_THREAD = "__converge_schema_probe__"


def configured_database_url() -> str | None:
    value = os.environ.get(_DATABASE_URL_ENV)
    return value.strip() if value and value.strip() else None


def _require_strict_msgpack() -> None:
    raw = os.environ.get(_STRICT_MSGPACK_ENV)
    if raw is None:
        os.environ[_STRICT_MSGPACK_ENV] = "true"
        return
    if raw.strip().lower() not in {"1", "true", "yes", "on"}:
        raise RuntimeError(
            "PostgreSQL persistence requires LANGGRAPH_STRICT_MSGPACK=true; "
            "unsafe checkpoint deserialization is not allowed"
        )


def _postgres_modules():
    try:
        import psycopg
        from langgraph.checkpoint.postgres import PostgresSaver
        from psycopg.rows import dict_row
    except ImportError as exc:  # pragma: no cover - optional dependency guard
        raise RuntimeError(
            "PostgreSQL persistence requires `pip install 'converge-orchestrator[postgres]'`"
        ) from exc
    return psycopg, PostgresSaver, dict_row


def open_checkpointer(
    state_dir: Path,
    database_url: str | None = None,
) -> tuple[Any, Any]:
    """Return `(checkpointer, closeable_connection)` for SQLite or shared PostgreSQL."""
    resolved = database_url or configured_database_url()
    if not resolved:
        db = sqlite3.connect(
            state_dir / "langgraph.sqlite",
            check_same_thread=False,
        )
        return SqliteSaver(db), db

    _require_strict_msgpack()
    psycopg, PostgresSaver, dict_row = _postgres_modules()
    db = psycopg.connect(
        resolved,
        autocommit=True,
        prepare_threshold=0,
        row_factory=dict_row,
    )
    return PostgresSaver(db), db


def _verify_postgres_checkpoint_schema(database_url: str) -> None:
    """Fail at service construction instead of failing the first autonomous run."""
    checkpointer, db = open_checkpointer(Path("."), database_url)
    try:
        checkpointer.get(
            {"configurable": {"thread_id": _SCHEMA_PROBE_THREAD}}
        )
    except Exception as exc:
        psycopg, _, _ = _postgres_modules()
        if isinstance(exc, psycopg.errors.UndefinedTable):
            raise RuntimeError(
                "PostgreSQL checkpoint schema is not initialized; run "
                "`converge persistence-setup` before starting workers"
            ) from exc
        raise
    finally:
        db.close()


class PersistenceBackend:
    """Select durable control/checkpoint storage without changing LangGraph workflow semantics."""

    def __init__(
        self,
        control_db_path: Path,
        database_url: str | None = None,
    ) -> None:
        self.control_db_path = control_db_path.expanduser().resolve()
        self.database_url = database_url or configured_database_url()
        if self.database_url:
            _require_strict_msgpack()
            self.registry = PostgresControlRegistry(self.database_url)
            _verify_postgres_checkpoint_schema(self.database_url)
        else:
            self.registry = ControlRegistry(self.control_db_path)

    @property
    def kind(self) -> str:
        return "postgres" if self.database_url else "sqlite"

    def open_checkpointer(self, state_dir: Path) -> tuple[Any, Any]:
        return open_checkpointer(state_dir, self.database_url)

    def is_database_error(self, exc: Exception) -> bool:
        if isinstance(exc, sqlite3.DatabaseError):
            return True
        if not self.database_url:
            return False
        psycopg, _, _ = _postgres_modules()
        return isinstance(exc, psycopg.Error)

    def is_transient_error(self, exc: Exception) -> bool:
        if isinstance(exc, sqlite3.OperationalError):
            message = str(exc).lower()
            return "locked" in message or "busy" in message
        if not self.database_url:
            return False
        psycopg, _, _ = _postgres_modules()
        transient_types = (
            psycopg.OperationalError,
            psycopg.errors.SerializationFailure,
            psycopg.errors.DeadlockDetected,
        )
        return isinstance(exc, transient_types)


def setup_postgres(database_url: str | None = None) -> None:
    """Create Converge control tables and run LangGraph's idempotent Postgres migrations."""
    resolved = database_url or configured_database_url()
    if not resolved:
        raise RuntimeError(f"{_DATABASE_URL_ENV} is not configured")
    _require_strict_msgpack()
    PostgresControlRegistry(resolved)
    _, PostgresSaver, _ = _postgres_modules()
    with PostgresSaver.from_conn_string(resolved) as checkpointer:
        checkpointer.setup()
