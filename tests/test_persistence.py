from __future__ import annotations

from pathlib import Path
from unittest.mock import Mock, patch

import pytest

from converge_orchestrator.persistence import (
    PersistenceBackend,
    configured_database_url,
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


def test_database_url_is_never_echoed_and_selects_postgres(tmp_path: Path, monkeypatch) -> None:
    database_url = "postgresql://user:secret@db.invalid/converge"
    monkeypatch.setenv("CONVERGE_DATABASE_URL", database_url)
    monkeypatch.delenv("LANGGRAPH_STRICT_MSGPACK", raising=False)
    registry = Mock()

    with patch(
        "converge_orchestrator.persistence.PostgresControlRegistry",
        return_value=registry,
    ) as registry_type:
        backend = PersistenceBackend(tmp_path / "unused.sqlite")

    assert configured_database_url() == database_url
    assert backend.kind == "postgres"
    assert backend.registry is registry
    registry_type.assert_called_once_with(database_url)
    assert "secret" not in repr(backend.registry)
    assert not (tmp_path / "unused.sqlite").exists()
    assert __import__("os").environ["LANGGRAPH_STRICT_MSGPACK"] == "true"


def test_unsafe_postgres_deserialization_setting_fails_closed(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("CONVERGE_DATABASE_URL", "postgresql://db/converge")
    monkeypatch.setenv("LANGGRAPH_STRICT_MSGPACK", "false")

    with pytest.raises(RuntimeError, match="STRICT_MSGPACK"):
        PersistenceBackend(tmp_path / "unused.sqlite")


def test_postgres_setup_requires_explicit_database_url(monkeypatch) -> None:
    monkeypatch.delenv("CONVERGE_DATABASE_URL", raising=False)

    with pytest.raises(RuntimeError, match="CONVERGE_DATABASE_URL"):
        setup_postgres()
