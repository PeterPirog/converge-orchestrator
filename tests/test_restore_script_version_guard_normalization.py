"""Restore-script version-guard normalization regression lock (REQ-BACKUP-001/003 compatibility).

Baseline blocker remediation evidence: canonical commit 2988e21355d35a3fd4f08062c1ea3b7a44e36b27
passed its required checks on 2026-11-07 and failed the identical rerun on 2026-11-10 because
pg_dump/pg_restore clients of major version >=17 (GitHub runner toolchain drift) emit the fixed
session directive ``SET transaction_timeout = 0;`` while the pinned supported restore target
(postgres:16-alpine, PostgreSQL 16.15) does not implement the parameter. The fail-closed restore
(``--set=ON_ERROR_STOP=1``) therefore aborts before any schema or data statement is applied.

The normalization removes only that complete, semantically neutral session-guard line from the
materialized restore script; every other byte of the materialization is unchanged.
"""

from __future__ import annotations

import inspect
from pathlib import Path

import pytest

from converge_orchestrator import restore_postgres

_MARKER = "RESTORE_SCRIPT_VERSION_GUARD_NORMALIZATION_MISSING"

_REPRESENTATIVE_SCRIPT = (
    b"--\n"
    b"-- PostgreSQL database dump\n"
    b"--\n"
    b"\n"
    b"SET statement_timeout = 0;\n"
    b"SET lock_timeout = 0;\n"
    b"SET idle_in_transaction_session_timeout = 0;\n"
    b"SET transaction_timeout = 0;\n"
    b"SET client_encoding = 'UTF8';\n"
    b"SET standard_conforming_strings = on;\n"
    b"\n"
    b"CREATE TABLE converge_checkpoint (id integer);\n"
    b"COPY converge_checkpoint (id) FROM stdin;\n"
    b"42\n"
    b"\\.\n"
    b"\n"
)


def test_version_guard_normalization_is_present() -> None:
    try:
        helper = restore_postgres._strip_unsupported_transaction_timeout_guard
    except AttributeError:
        pytest.fail(_MARKER)
    assert callable(helper)


def test_guard_line_removed_and_rest_byte_identical(tmp_path: Path) -> None:
    script = tmp_path / "restore.sql"
    script.write_bytes(_REPRESENTATIVE_SCRIPT)

    removed = restore_postgres._strip_unsupported_transaction_timeout_guard(script)

    assert removed == 1
    normalized = script.read_bytes()
    assert b"transaction_timeout" not in normalized
    expected = _REPRESENTATIVE_SCRIPT.replace(
        b"SET transaction_timeout = 0;\n", b""
    )
    assert normalized == expected


def test_normalization_is_idempotent(tmp_path: Path) -> None:
    script = tmp_path / "restore.sql"
    script.write_bytes(_REPRESENTATIVE_SCRIPT)

    first = restore_postgres._strip_unsupported_transaction_timeout_guard(script)
    snapshot = script.read_bytes()
    script.write_bytes(snapshot)
    second = restore_postgres._strip_unsupported_transaction_timeout_guard(script)

    assert first == 1
    assert second == 0
    assert script.read_bytes() == snapshot


def test_script_without_guard_is_not_modified(tmp_path: Path) -> None:
    script = tmp_path / "restore.sql"
    script.write_bytes(_REPRESENTATIVE_SCRIPT.replace(b"SET transaction_timeout = 0;\n", b""))
    before = script.read_bytes()

    removed = restore_postgres._strip_unsupported_transaction_timeout_guard(script)

    assert removed == 0
    assert script.read_bytes() == before


def test_receipt_tail_is_preserved(tmp_path: Path) -> None:
    script = tmp_path / "restore.sql"
    payload = _REPRESENTATIVE_SCRIPT + b"\n-- converge-restore-receipt\nSELECT 1;\n"
    script.write_bytes(payload)

    restore_postgres._strip_unsupported_transaction_timeout_guard(script)

    normalized = script.read_bytes()
    assert b"-- converge-restore-receipt\nSELECT 1;\n" in normalized
    assert b"transaction_timeout" not in normalized


def test_normalization_runs_before_receipt_append() -> None:
    source = inspect.getsource(restore_postgres._materialize_restore_script)
    strip_at = source.index("_strip_unsupported_transaction_timeout_guard(script)")
    receipt_at = source.index("_receipt_sql(")
    assert strip_at < receipt_at
