from __future__ import annotations

import sqlite3
import time
from pathlib import Path
from unittest.mock import Mock, patch

import pytest

from converge_orchestrator.persistence import (
    PersistenceBackend,
    configured_database_url,
    open_checkpointer,
    setup_postgres,
)
from converge_orchestrator.registry import ControlRegistry


def test_sqlite_remains_default_without_database_url(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.delenv("CONVERGE_DATABASE_URL", raising=False)
    monkeypatch.delenv("LANGGRAPH_STRICT_MSGPACK", raising=False)

    backend = PersistenceBackend(tmp_path / "control.sqlite")

    assert backend.kind == "sqlite"
    assert isinstance(backend.registry, ControlRegistry)
    checkpointer, db = backend.open_checkpointer(tmp_path)
    try:
        assert checkpointer is not None
    finally:
        db.close()
    assert (tmp_path / "langgraph.sqlite").is_file()


def test_database_url_selects_postgres_without_materializing_secret(
    tmp_path: Path,
    monkeypatch,
) -> None:
    database_url = "postgresql://user:secret@db.invalid/converge"
    monkeypatch.setenv("CONVERGE_DATABASE_URL", database_url)
    monkeypatch.delenv("LANGGRAPH_STRICT_MSGPACK", raising=False)
    registry = Mock()

    with (
        patch(
            "converge_orchestrator.persistence.PostgresControlRegistry",
            return_value=registry,
        ) as registry_type,
        patch("converge_orchestrator.persistence._verify_postgres_checkpoint_schema") as verify,
    ):
        backend = PersistenceBackend(tmp_path / "unused.sqlite")

    assert configured_database_url() == database_url
    assert backend.kind == "postgres"
    assert backend.registry is registry
    registry_type.assert_called_once_with(database_url)
    verify.assert_called_once_with(database_url)
    assert not (tmp_path / "unused.sqlite").exists()
    assert __import__("os").environ["LANGGRAPH_STRICT_MSGPACK"] == "true"


def test_missing_postgres_checkpoint_schema_fails_before_controller_runs(
    tmp_path: Path,
    monkeypatch,
) -> None:
    database_url = "postgresql://db.invalid/converge"
    monkeypatch.setenv("CONVERGE_DATABASE_URL", database_url)
    monkeypatch.delenv("LANGGRAPH_STRICT_MSGPACK", raising=False)

    with (
        patch("converge_orchestrator.persistence.PostgresControlRegistry"),
        patch(
            "converge_orchestrator.persistence._verify_postgres_checkpoint_schema",
            side_effect=RuntimeError("run `converge persistence-setup`"),
        ),
        pytest.raises(RuntimeError, match="persistence-setup"),
    ):
        PersistenceBackend(tmp_path / "unused.sqlite")


def test_unsafe_postgres_deserialization_setting_fails_closed(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("CONVERGE_DATABASE_URL", "postgresql://db/converge")
    monkeypatch.setenv("LANGGRAPH_STRICT_MSGPACK", "false")

    with pytest.raises(RuntimeError, match="STRICT_MSGPACK"):
        PersistenceBackend(tmp_path / "unused.sqlite")


def test_postgres_setup_requires_explicit_database_url(monkeypatch) -> None:
    monkeypatch.delenv("CONVERGE_DATABASE_URL", raising=False)

    with pytest.raises(RuntimeError, match="CONVERGE_DATABASE_URL"):
        setup_postgres()


def test_checkpoint_sqlite_uses_wal_with_bounded_busy_timeout(
    tmp_path: Path,
    monkeypatch,
) -> None:
    monkeypatch.delenv("CONVERGE_DATABASE_URL", raising=False)

    checkpointer, db = open_checkpointer(tmp_path)
    try:
        assert db.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
        assert db.execute("PRAGMA busy_timeout").fetchone()[0] == 30000
    finally:
        db.close()


def test_checkpoint_sqlite_readers_stay_responsive_during_writer_lock(
    tmp_path: Path,
    monkeypatch,
) -> None:
    """A checkpoint writer must never block status readers (external acceptance V20 stall).

    Before the WAL fix, langgraph.sqlite ran in rollback-journal mode: a workflow-thread write
    transaction took the database EXCLUSIVE lock and status-poll readers blocked (or errored)
    behind it, stalling the supervisor's 15s-timeout GET. WAL lets readers proceed on the last
    committed snapshot while a writer holds its exclusive transaction.
    """
    monkeypatch.delenv("CONVERGE_DATABASE_URL", raising=False)

    checkpointer, db = open_checkpointer(tmp_path)
    try:
        path = tmp_path / "langgraph.sqlite"
        writer = sqlite3.connect(path, timeout=0.1)
        reader = sqlite3.connect(path, timeout=0.1)
        try:
            writer.execute("PRAGMA busy_timeout = 100")
            writer.execute("BEGIN EXCLUSIVE")
            reader.execute("PRAGMA busy_timeout = 100")
            started = time.monotonic()
            rows = reader.execute("SELECT count(*) FROM sqlite_master").fetchone()
            elapsed = time.monotonic() - started
            assert rows[0] == 0
            assert elapsed < 2.0, "checkpoint reader was blocked behind the writer"
        finally:
            reader.close()
            writer.rollback()
            writer.close()
    finally:
        db.close()
