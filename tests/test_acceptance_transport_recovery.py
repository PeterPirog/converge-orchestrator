from __future__ import annotations

import io
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
from urllib.error import HTTPError

import pytest

from converge_orchestrator.acceptance_supervisor import (
    _API_RETRY_BACKOFF_SECONDS,
    AcceptanceSupervisorError,
    _api_json,
    _append_jsonl_line,
    _transport_attempt_sink,
    _write_failure_record,
)

_BASE_URL = "http://127.0.0.1:1"


class _FakeResponse:
    def __init__(self, payload: bytes) -> None:
        self._payload = payload

    def __enter__(self) -> _FakeResponse:
        return self

    def __exit__(self, *args: object) -> bool:
        return False

    def read(self) -> bytes:
        return self._payload


def _http_error(code: int) -> HTTPError:
    return HTTPError(
        f"{_BASE_URL}/runs/run-1", code, "transport gateway", None, io.BytesIO(b"body")
    )


def test_transient_get_timeout_recovers_inside_the_same_call() -> None:
    """Test A: one transient API read timeout -> bounded retry -> same call succeeds."""
    calls: list[str] = []
    delays: list[float] = []
    records: list[dict] = []

    def flaky_urlopen(request, timeout=None):  # noqa: ANN001, ARG001
        calls.append(request.get_method())
        if len(calls) < 3:
            raise TimeoutError("timed out")
        return _FakeResponse(b'{"ok": true}')

    with (
        patch(
            "converge_orchestrator.acceptance_supervisor.urlopen",
            side_effect=flaky_urlopen,
        ),
        _transport_attempt_sink(records.append),
    ):
        result = _api_json(_BASE_URL, "token", "GET", "/runs/run-1", sleeper=delays.append)

    assert result == {"ok": True}
    assert calls == ["GET", "GET", "GET"]
    assert delays == list(_API_RETRY_BACKOFF_SECONDS[:2])
    assert [(item["attempt"], item["outcome"], item["failure_class"]) for item in records] == [
        (1, "retry", "timeout"),
        (2, "retry", "timeout"),
        (3, "recovered", None),
    ]


def test_repeated_get_timeouts_fail_closed_with_structured_record() -> None:
    """Test B: timeouts beyond the retry budget -> finite deterministic failure + evidence."""
    records: list[dict] = []

    def dead_urlopen(request, timeout=None):  # noqa: ANN001, ARG001
        raise TimeoutError("timed out")

    with (
        patch(
            "converge_orchestrator.acceptance_supervisor.urlopen",
            side_effect=dead_urlopen,
        ),
        _transport_attempt_sink(records.append),
    ):
        with pytest.raises(AcceptanceSupervisorError) as exc_info:
            _api_json(_BASE_URL, "token", "GET", "/runs/run-1", sleeper=lambda _s: None)

    exc = exc_info.value
    assert exc.failure_kind == "transport_exhausted"
    attempts = exc.transport_attempts
    assert attempts is not None
    assert [item["attempt"] for item in attempts] == [1, 2, 3, 4]
    assert [item["outcome"] for item in attempts] == ["retry", "retry", "retry", "exhausted"]
    assert all(item["failure_class"] == "timeout" for item in attempts)
    # Retry evidence only ever concerns the idempotent read; no side-effecting call appears.
    assert all(
        item["method"] == "GET" and item["path"] == "/runs/run-1" for item in attempts
    )
    assert [(item["attempt"], item["outcome"]) for item in records] == [
        (1, "retry"),
        (2, "retry"),
        (3, "retry"),
        (4, "exhausted"),
    ]


def test_operator_decision_post_is_never_replayed_after_transport_timeout() -> None:
    """Test C: an ambiguous operator-decision POST fails closed with exactly one attempt."""
    calls: list[str] = []

    def dead_urlopen(request, timeout=None):  # noqa: ANN001, ARG001
        calls.append(request.get_method())
        raise TimeoutError("timed out")

    with patch(
        "converge_orchestrator.acceptance_supervisor.urlopen", side_effect=dead_urlopen
    ):
        with pytest.raises(AcceptanceSupervisorError) as exc_info:
            _api_json(
                _BASE_URL,
                "token",
                "POST",
                "/runs/run-1/decision",
                {"action": "approve"},
            )

    assert calls == ["POST"]
    exc = exc_info.value
    assert exc.failure_kind == "transport_exhausted"
    assert exc.transport_attempts == [
        {
            "attempt": 1,
            "method": "POST",
            "path": "/runs/run-1/decision",
            "failure_class": "timeout",
            "delay_seconds": None,
            "outcome": "not_retried",
        }
    ]


def test_get_502_is_retried_and_recovers() -> None:
    calls: list[str] = []
    delays: list[float] = []

    def flaky_urlopen(request, timeout=None):  # noqa: ANN001, ARG001
        calls.append(request.get_method())
        if len(calls) == 1:
            raise _http_error(502)
        return _FakeResponse(b'{"ok": true}')

    with patch(
        "converge_orchestrator.acceptance_supervisor.urlopen", side_effect=flaky_urlopen
    ):
        result = _api_json(_BASE_URL, "token", "GET", "/runs/run-1", sleeper=delays.append)

    assert result == {"ok": True}
    assert calls == ["GET", "GET"]
    assert delays == [1.0]


def test_post_502_is_never_retried() -> None:
    calls: list[str] = []

    def dead_urlopen(request, timeout=None):  # noqa: ANN001, ARG001
        calls.append(request.get_method())
        raise _http_error(502)

    with patch(
        "converge_orchestrator.acceptance_supervisor.urlopen", side_effect=dead_urlopen
    ):
        with pytest.raises(AcceptanceSupervisorError) as exc_info:
            _api_json(_BASE_URL, "token", "POST", "/projects/p/run")

    assert calls == ["POST"]
    assert exc_info.value.failure_kind == "transport_exhausted"
    assert exc_info.value.transport_attempts[-1]["outcome"] == "not_retried"


def test_non_retryable_http_status_fails_closed_without_retry() -> None:
    calls: list[str] = []
    delays: list[float] = []

    def refusing_urlopen(request, timeout=None):  # noqa: ANN001, ARG001
        calls.append(request.get_method())
        raise _http_error(404)

    with patch(
        "converge_orchestrator.acceptance_supervisor.urlopen", side_effect=refusing_urlopen
    ):
        with pytest.raises(AcceptanceSupervisorError) as exc_info:
            _api_json(_BASE_URL, "token", "GET", "/runs/run-1", sleeper=delays.append)

    assert calls == ["GET"]
    assert delays == []
    exc = exc_info.value
    assert exc.failure_kind is None
    assert exc.transport_attempts is None


def test_transport_failure_record_carries_the_attempt_evidence(tmp_path: Path) -> None:
    """Exhaustion evidence must survive into the durable failure record."""
    config = SimpleNamespace(state_dir=tmp_path / "state", github_repo="example/target")
    attempts = [
        {
            "attempt": 1,
            "method": "GET",
            "path": "/runs/run-1",
            "failure_class": "timeout",
            "delay_seconds": 1.0,
            "outcome": "retry",
        }
    ]
    exc = AcceptanceSupervisorError(
        "transport recovery exhausted",
        failure_kind="transport_exhausted",
        transport_attempts=attempts,
    )
    output_path = tmp_path / "acceptance-supervisor.json"

    _write_failure_record(
        output_path=output_path,
        config=config,
        project_id="external-acceptance",
        run_id="run-1",
        exc=exc,
        progress=None,
        expected_risk_flag="forbidden_public_api_change",
    )

    payload = json.loads(output_path.read_text(encoding="utf-8"))
    assert payload["failure_kind"] == "transport_exhausted"
    assert payload["transport_attempts"] == attempts
    evidence_copy = (
        config.state_dir / "evidence" / "run-1" / "external-acceptance-failure.json"
    )
    assert json.loads(evidence_copy.read_text(encoding="utf-8"))["transport_attempts"] == attempts


def test_transport_attempts_are_persisted_as_jsonl_lines(tmp_path: Path) -> None:
    log_path = tmp_path / "acceptance" / "run-1" / "transport-attempts.jsonl"
    _append_jsonl_line(log_path, {"attempt": 1, "outcome": "retry"})
    _append_jsonl_line(log_path, {"attempt": 2, "outcome": "exhausted"})

    lines = log_path.read_text(encoding="utf-8").splitlines()
    assert [json.loads(line) for line in lines] == [
        {"attempt": 1, "outcome": "retry"},
        {"attempt": 2, "outcome": "exhausted"},
    ]