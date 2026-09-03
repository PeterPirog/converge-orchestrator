from __future__ import annotations

import types
from pathlib import Path
from unittest.mock import Mock, patch

from converge_orchestrator.ci import ci_poll, route_after_ci
from converge_orchestrator.ci_flakes import FlakyCIPolicy
from converge_orchestrator.github import GitHubError
from converge_orchestrator.models import CIResult, ProjectConfig, TaskEnvelope


def _config(tmp_path: Path) -> ProjectConfig:
    return ProjectConfig(
        repo_path=tmp_path,
        requirements_path=tmp_path / "architecture.md",
        github_repo="owner/repo",
        agents={},
        ci_timeout_seconds=1800,
    )


def _state() -> dict:
    task = TaskEnvelope(
        id="ARCH-001-1",
        requirement_ids=["ARCH-001"],
        title="Change",
        objective="Change safely",
    )
    return {
        "config_path": "converge.yaml",
        "run_id": "run-1",
        "task": task.model_dump(mode="json"),
        "pr": {
            "number": 1,
            "url": "https://example.test/pr/1",
            "head_sha": "abc",
        },
        "repair_attempts": 0,
        "replan_attempts": 0,
    }


def _failed_ci(name: str = "CI") -> CIResult:
    return CIResult(
        status="fail",
        head_sha="abc",
        checks=[
            {
                "kind": "remote_policy",
                "authoritative": True,
                "required_checks": [{"context": name, "app_id": None}],
            },
            {
                "kind": "check_run",
                "name": name,
                "status": "completed",
                "conclusion": "failure",
                "required": True,
            },
        ],
    )


def _store(ledger: dict | None = None):
    return types.SimpleNamespace(
        write_json=Mock(),
        append_event=Mock(),
        read_json=Mock(return_value=ledger),
    )


def _invoke(
    tmp_path: Path,
    state: dict,
    adapter: Mock,
    policy: FlakyCIPolicy,
    *,
    store=None,
) -> dict:
    cfg = _config(tmp_path)
    evidence = store or _store()
    with (
        patch("converge_orchestrator.ci.load_config", return_value=cfg),
        patch("converge_orchestrator.ci.load_flaky_ci_policy", return_value=policy),
        patch("converge_orchestrator.ci.wf._evidence", return_value=evidence),
        patch("converge_orchestrator.ci.GitHubAdapter", return_value=adapter),
    ):
        return ci_poll(state)


def test_explicit_flaky_failure_is_rerun_and_returns_to_machine_wait(tmp_path: Path) -> None:
    adapter = Mock()
    adapter.ci_status.return_value = _failed_ci()
    adapter.rerun_failed_actions_check.return_value = 456
    policy = FlakyCIPolicy(checks=["CI"], max_retries_per_check=1)

    result = _invoke(tmp_path, _state(), adapter, policy)

    adapter.rerun_failed_actions_check.assert_called_once_with("abc", "CI")
    assert result["ci"]["status"] == "pending"
    assert result["ci_flaky_retries"] == {"CI": 1}
    assert result["ci_flaky_retry_error"] is None
    assert route_after_ci(result) == "wait"


def test_exhausted_flaky_retry_budget_falls_back_to_normal_repair(tmp_path: Path) -> None:
    adapter = Mock()
    adapter.ci_status.return_value = _failed_ci()
    policy = FlakyCIPolicy(checks=["CI"], max_retries_per_check=1)
    state = _state()
    state["ci_head_sha"] = "abc"
    state["ci_flaky_retries"] = {"CI": 1}

    result = _invoke(tmp_path, state, adapter, policy)

    adapter.rerun_failed_actions_check.assert_not_called()
    with patch("converge_orchestrator.ci.load_config", return_value=_config(tmp_path)):
        assert route_after_ci(result) == "repair"


def test_new_candidate_head_resets_flaky_retry_ledger(tmp_path: Path) -> None:
    adapter = Mock()
    adapter.ci_status.return_value = _failed_ci()
    adapter.rerun_failed_actions_check.return_value = 456
    policy = FlakyCIPolicy(checks=["CI"], max_retries_per_check=1)
    state = _state()
    state["ci_head_sha"] = "old-head"
    state["ci_flaky_retries"] = {"CI": 1}

    result = _invoke(tmp_path, state, adapter, policy)

    adapter.rerun_failed_actions_check.assert_called_once_with("abc", "CI")
    assert result["ci_flaky_retries"] == {"CI": 1}


def test_uncheckpointed_retry_reservation_is_not_duplicated_after_restart(tmp_path: Path) -> None:
    adapter = Mock()
    adapter.ci_status.return_value = _failed_ci()
    policy = FlakyCIPolicy(checks=["CI"], max_retries_per_check=1)
    store = _store({"head_sha": "abc", "counts": {"CI": 1}})

    result = _invoke(tmp_path, _state(), adapter, policy, store=store)

    adapter.rerun_failed_actions_check.assert_not_called()
    assert result["ci"]["status"] == "pending"
    assert result["ci_flaky_retries"] == {"CI": 1}
    assert route_after_ci(result) == "wait"
    assert any(
        call.args[1] == "ci_flaky_recovery_wait"
        for call in store.append_event.call_args_list
    )


def test_rerun_transport_failure_preserves_reservation_before_escalation(tmp_path: Path) -> None:
    adapter = Mock()
    adapter.ci_status.return_value = _failed_ci()
    policy = FlakyCIPolicy(checks=["CI"], max_retries_per_check=1)
    store = _store()

    def fail_after_reservation(*_args):
        ledger_write = store.write_json.call_args_list[0]
        assert ledger_write.args[2] == "ci-flaky-retries.json"
        assert ledger_write.args[3] == {"head_sha": "abc", "counts": {"CI": 1}}
        raise GitHubError("actions rerun forbidden")

    adapter.rerun_failed_actions_check.side_effect = fail_after_reservation

    result = _invoke(tmp_path, _state(), adapter, policy, store=store)

    assert result["ci"]["status"] == "fail"
    assert result["ci_flaky_retries"] == {"CI": 1}
    assert "actions rerun forbidden" in result["ci_flaky_retry_error"]
    assert route_after_ci(result) == "human"
