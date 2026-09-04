from __future__ import annotations

import os
from collections.abc import Mapping

_LIBPQ_ENV = {
    "application_name": "PGAPPNAME",
    "channel_binding": "PGCHANNELBINDING",
    "client_encoding": "PGCLIENTENCODING",
    "connect_timeout": "PGCONNECT_TIMEOUT",
    "dbname": "PGDATABASE",
    "gssencmode": "PGGSSENCMODE",
    "host": "PGHOST",
    "hostaddr": "PGHOSTADDR",
    "keepalives": "PGKEEPALIVES",
    "keepalives_count": "PGKEEPALIVESCOUNT",
    "keepalives_idle": "PGKEEPALIVESIDLE",
    "keepalives_interval": "PGKEEPALIVESINTERVAL",
    "krbsrvname": "PGKRBSRVNAME",
    "load_balance_hosts": "PGLOADBALANCEHOSTS",
    "options": "PGOPTIONS",
    "passfile": "PGPASSFILE",
    "password": "PGPASSWORD",
    "port": "PGPORT",
    "replication": "PGREPLICATION",
    "requirepeer": "PGREQUIREPEER",
    "service": "PGSERVICE",
    "servicefile": "PGSERVICEFILE",
    "sslcert": "PGSSLCERT",
    "sslcompression": "PGSSLCOMPRESSION",
    "sslcrl": "PGSSLCRL",
    "sslcrldir": "PGSSLCRLDIR",
    "sslkey": "PGSSLKEY",
    "sslmode": "PGSSLMODE",
    "sslpassword": "PGSSLPASSWORD",
    "sslrootcert": "PGSSLROOTCERT",
    "sslsni": "PGSSLSNI",
    "ssl_min_protocol_version": "PGSSLMINPROTOCOLVERSION",
    "ssl_max_protocol_version": "PGSSLMAXPROTOCOLVERSION",
    "target_session_attrs": "PGTARGETSESSIONATTRS",
    "tcp_user_timeout": "PGTCPUSER_TIMEOUT",
    "user": "PGUSER",
}


def _conninfo_to_dict(database_url: str) -> dict[str, str]:
    try:
        from psycopg.conninfo import conninfo_to_dict
    except ImportError as exc:  # pragma: no cover - optional dependency guard
        raise RuntimeError(
            "PostgreSQL CLI operations require `pip install 'converge-orchestrator[postgres]'`"
        ) from exc
    try:
        return {
            str(key): str(value)
            for key, value in conninfo_to_dict(database_url).items()
            if value not in (None, "")
        }
    except Exception as exc:
        raise RuntimeError("PostgreSQL connection configuration could not be parsed") from exc


def libpq_env(
    database_url: str,
    *,
    base: Mapping[str, str] | None = None,
) -> dict[str, str]:
    """Convert a URI/conninfo into isolated libpq environment variables without argv secrets."""
    parsed = _conninfo_to_dict(database_url)
    unsupported = sorted(set(parsed) - set(_LIBPQ_ENV))
    if unsupported:
        raise RuntimeError(
            "PostgreSQL connection configuration contains CLI-unsupported options: "
            + ", ".join(unsupported)
        )

    env = dict(os.environ if base is None else base)
    for variable in set(_LIBPQ_ENV.values()):
        env.pop(variable, None)
    for key, value in parsed.items():
        env[_LIBPQ_ENV[key]] = value
    if not env.get("PGDATABASE") and not env.get("PGSERVICE"):
        raise RuntimeError("PostgreSQL connection configuration has no database or service target")
    return env
