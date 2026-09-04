from __future__ import annotations

from unittest.mock import patch

from converge_orchestrator import postgres_client


def test_libpq_env_replaces_inherited_connection_state() -> None:
    parsed = {
        "host": "database",
        "dbname": "converge",
        "user": "converge-user",
        "password": "super-secret",
        "sslmode": "require",
    }
    inherited = {
        "PATH": "/usr/bin",
        "PGHOST": "stale-host",
        "PGDATABASE": "stale-db",
        "PGUSER": "stale-user",
        "PGPASSWORD": "stale-secret",
        "PGSERVICE": "stale-service",
        "PGSSLMODE": "disable",
    }

    with patch.object(postgres_client, "_conninfo_to_dict", return_value=parsed):
        env = postgres_client.libpq_env("ignored", base=inherited)

    assert env["PATH"] == "/usr/bin"
    assert env["PGHOST"] == "database"
    assert env["PGDATABASE"] == "converge"
    assert env["PGUSER"] == "converge-user"
    assert env["PGPASSWORD"] == "super-secret"
    assert env["PGSSLMODE"] == "require"
    assert "PGSERVICE" not in env
