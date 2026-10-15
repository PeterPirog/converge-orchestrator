"""Regression lock for INV-SEC-001 (secret non-persistence).

Configured secret values (a model-gateway API key and an MCP header secret) are supplied
only through process environment variables. This module proves they are never materialized
into the generated OpenCode config, the pinned run-config snapshot, the inline agent
runtime config, or the durable context/provider ledgers -- only env references/names
(``{env:NAME}`` / ``NAME``) are stored. Fully offline: no network, no OpenCode binary.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

import yaml

from converge_orchestrator.budget import initialize_run_budget, reserve_model_attempt
from converge_orchestrator.config import (
    load_config,
    load_run_config_snapshot,
    materialize_run_config_snapshot,
)
from converge_orchestrator.context import (
    append_context_ledger,
    finalize_report,
    prepare_prompt,
)
from converge_orchestrator.model_usage import parse_opencode_output, record_model_usage_attempt
from converge_orchestrator.opencode_config import (
    build_opencode_config,
    materialize_opencode_config,
    runtime_opencode_config,
)
from converge_orchestrator.sandbox import _configured_env_names

GATEWAY_KEY_ENV = "CONVERGE_INV_SEC_001_GATEWAY_KEY"
MCP_HEADER_ENV = "CONVERGE_INV_SEC_001_MCP_HEADER"
GATEWAY_KEY_VALUE = "gateway-secret-value-inv-sec-001-a1b2c3d4"
MCP_HEADER_VALUE = "mcp-header-secret-value-inv-sec-001-e5f6a7b8"


def _set_secrets(monkeypatch) -> None:
    monkeypatch.setenv(GATEWAY_KEY_ENV, GATEWAY_KEY_VALUE)
    monkeypatch.setenv(MCP_HEADER_ENV, MCP_HEADER_VALUE)


def _assert_secrets_absent(blob: str, surface: str) -> None:
    assert GATEWAY_KEY_VALUE not in blob, f"gateway key value leaked into {surface}"
    assert MCP_HEADER_VALUE not in blob, f"MCP header value leaked into {surface}"


def _write_fixture(tmp_path: Path) -> Path:
    (tmp_path / "repo").mkdir()
    (tmp_path / "requirements.md").write_text("# Requirements\n", encoding="utf-8")
    config = {
        "version": 1,
        "project": {
            "repo_path": "repo",
            "requirements_path": "requirements.md",
            "state_dir": "state",
        },
        "models": {
            "gateway": {
                "kind": "openwebui",
                "base_url": "http://127.0.0.1:3000/api",
                "api_key_env": GATEWAY_KEY_ENV,
            },
            "profiles": {
                "default": {"model": "test-model", "context_tokens": 128000, "output_tokens": 4096},
            },
        },
        "mcp": {
            "servers": {
                "docs": {
                    "type": "remote",
                    "enabled": True,
                    "headers": {"X-Api-Key": f"{{env:{MCP_HEADER_ENV}}}"},
                },
            },
        },
        "agents": {
            "builder": {
                "agent": "converge-builder",
                "model_profile": "default",
                "tool_permissions": {"docs_*": "allow"},
            },
        },
    }
    path = tmp_path / "converge.yaml"
    path.write_text(yaml.safe_dump(config, sort_keys=False), encoding="utf-8")
    return path


def test_generated_and_inline_opencode_configs_store_env_references_not_values(
    tmp_path: Path,
    monkeypatch,
) -> None:
    _set_secrets(monkeypatch)
    cfg = load_config(_write_fixture(tmp_path))

    baseline = materialize_opencode_config(cfg).read_text(encoding="utf-8")
    _assert_secrets_absent(baseline, "generated OpenCode config")
    assert f"{{env:{GATEWAY_KEY_ENV}}}" in baseline
    assert f"{{env:{MCP_HEADER_ENV}}}" in baseline

    inline = json.dumps(runtime_opencode_config(cfg, active_role="builder"), separators=(",", ":"))
    _assert_secrets_absent(inline, "inline agent runtime config")
    assert f"{{env:{GATEWAY_KEY_ENV}}}" in inline
    assert f"{{env:{MCP_HEADER_ENV}}}" in inline

    payload = build_opencode_config(cfg, active_role="builder")
    options = payload["provider"]["openwebui"]["options"]
    assert options["apiKey"] == f"{{env:{GATEWAY_KEY_ENV}}}"
    assert payload["mcp"]["docs"]["headers"]["X-Api-Key"] == f"{{env:{MCP_HEADER_ENV}}}"


def test_run_config_snapshot_pins_env_names_not_secret_values(tmp_path: Path, monkeypatch) -> None:
    _set_secrets(monkeypatch)
    source = _write_fixture(tmp_path)

    _, snapshot, digest = materialize_run_config_snapshot(source, "inv-sec-001")
    content = snapshot.read_text(encoding="utf-8")
    _assert_secrets_absent(content, "pinned run configuration snapshot")
    assert f"api_key_env: {GATEWAY_KEY_ENV}" in content
    assert f"{{env:{MCP_HEADER_ENV}}}" in content

    reloaded = load_run_config_snapshot(snapshot, digest)
    assert reloaded.model_gateway.api_key_env == GATEWAY_KEY_ENV
    assert reloaded.mcp["servers"]["docs"]["headers"]["X-Api-Key"] == f"{{env:{MCP_HEADER_ENV}}}"


def test_durable_context_ledger_stores_metadata_only(tmp_path: Path, monkeypatch) -> None:
    _set_secrets(monkeypatch)
    cfg = load_config(_write_fixture(tmp_path))

    _rendered, report = prepare_prompt(cfg, "builder", "Implement the assigned task.")
    append_context_ledger(cfg, finalize_report(report, "Done."), cfg.repo_path)

    ledger = (cfg.state_dir / "context-usage.jsonl").read_text(encoding="utf-8")
    _assert_secrets_absent(ledger, "durable context ledger")
    line = json.loads(ledger.strip().splitlines()[-1])
    assert line["role"] == "builder"
    assert {
        "timestamp", "cwd", "invocation_id", "role", "session_mode", "budget_status",
        "context_limit_tokens", "output_reserve_tokens", "input_budget_tokens",
        "estimated_core_tokens", "estimated_input_tokens", "estimated_output_tokens",
        "advisory_included", "advisory_truncated", "advisory_dropped", "estimator",
    } <= set(line)


def test_durable_provider_usage_ledger_omits_secret_values(tmp_path: Path, monkeypatch) -> None:
    _set_secrets(monkeypatch)
    cfg = load_config(_write_fixture(tmp_path))
    run_id = "inv-sec-001"

    initialize_run_budget(cfg, run_id, started_at=datetime.now(UTC))
    reservation = reserve_model_attempt(
        cfg,
        run_id,
        role="builder",
        model="openwebui/test-model",
        estimated_input_tokens=16,
        output_reserve_tokens=4096,
    )
    parsed = parse_opencode_output("Task completed deterministically.")
    record = record_model_usage_attempt(
        cfg,
        run_id,
        reservation_attempt=int(reservation["attempt"]),
        role="builder",
        model="openwebui/test-model",
        model_profile="default",
        returncode=0,
        parsed=parsed,
    )

    attempt_name = f"attempt-{record.reservation_attempt:06d}.json"
    attempt = cfg.state_dir / "evidence" / run_id / "model-usage" / attempt_name
    blob = attempt.read_text(encoding="utf-8")
    _assert_secrets_absent(blob, "durable provider-usage ledger")
    payload = json.loads(blob)
    assert payload["role"] == "builder"
    assert payload["model"] == "openwebui/test-model"
    assert {
        "version", "run_id", "reservation_attempt", "role", "model", "model_profile",
        "returncode", "usage_status", "usage", "recorded_at",
    } <= set(payload)

    budget = (cfg.state_dir / "evidence" / run_id / "run-budget.json").read_text(encoding="utf-8")
    _assert_secrets_absent(budget, "durable run budget ledger")


def test_sandbox_env_discovery_forwards_names_only(tmp_path: Path, monkeypatch) -> None:
    _set_secrets(monkeypatch)
    cfg = load_config(_write_fixture(tmp_path))

    names = _configured_env_names(cfg, "agent", "builder")
    assert names == {GATEWAY_KEY_ENV, MCP_HEADER_ENV}
