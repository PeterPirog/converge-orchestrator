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
