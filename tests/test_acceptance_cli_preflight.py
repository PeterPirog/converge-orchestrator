from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

import pytest
import typer
from typer.testing import CliRunner

from converge_orchestrator.acceptance_cli import _acceptance_preflight, app, supervise
from converge_orchestrator.acceptance_supervisor import AcceptanceSupervisorError
from converge_orchestrator.github import GitHubError, RemotePolicy, RequiredCheck


def _config() -> SimpleNamespace:
    return SimpleNamespace(
        github_repo="example/target",
        base_branch="main",
    )


def test_acceptance_preflight_requires_authoritative_required_ci(
    tmp_path: Path, monkeypatch
) -> None:
    config = _config()
    policy = RemotePolicy(
        base_branch="main",
        protected=True,
        authoritative=True,
        source="rulesets",
        required_checks=(RequiredCheck(context="test (3.13)", app_id=15368),),
        strict=True,
    )
    adapter = Mock()
    adapter.remote_policy.return_value = policy
    # Set explicit control DB for SQLite mode acceptance
    explicit_db = tmp_path / "control.sqlite"
    explicit_db.touch()
    monkeypatch.setenv("CONVERGE_CONTROL_DB", str(explicit_db))
    monkeypatch.delenv("CONVERGE_DATABASE_URL", raising=False)

    with (
        patch("converge_orchestrator.acceptance_cli.load_config", return_value=config),
        patch("converge_orchestrator.acceptance_cli._validate_acceptance_preconditions"),
        patch("converge_orchestrator.acceptance_cli.GitHubAdapter", return_value=adapter),
    ):
        result = _acceptance_preflight(tmp_path / "converge.yaml")

    adapter.remote_policy.assert_called_once_with("main")
    assert result == {
        "target_repository": "example/target",
        "base_branch": "main",
        "policy_source": "rulesets",
        "strict": True,
        "required_checks": [{"context": "test (3.13)", "app_id": 15368}],
        "control_backend": {
            "kind": "sqlite",
            "db_path": str(explicit_db.resolve()),
        },
    }


@pytest.mark.parametrize(
    "policy, expected",
    [
        (
            RemotePolicy(
                base_branch="main",
                protected=True,
                authoritative=False,
                source="protected_policy_unavailable",
            ),
            "not authoritative",
        ),
        (
            RemotePolicy(
                base_branch="main",
                protected=False,
                authoritative=True,
                source="branch_unprotected",
            ),
            "at least one authoritative GitHub status check",
        ),
        (
            RemotePolicy(
                base_branch="main",
                protected=True,
                authoritative=True,
                source="protected_no_required_status_checks",
            ),
            "at least one authoritative GitHub status check",
        ),
    ],
)
def test_acceptance_preflight_fails_closed_on_non_enforcing_ci(
    tmp_path: Path,
    policy: RemotePolicy,
    expected: str,
) -> None:
    config = _config()
    adapter = Mock()
    adapter.remote_policy.return_value = policy

    with (
        patch("converge_orchestrator.acceptance_cli.load_config", return_value=config),
        patch("converge_orchestrator.acceptance_cli._validate_acceptance_preconditions"),
        patch("converge_orchestrator.acceptance_cli.GitHubAdapter", return_value=adapter),
        pytest.raises(AcceptanceSupervisorError, match=expected),
    ):
        _acceptance_preflight(tmp_path / "converge.yaml")


def test_acceptance_preflight_wraps_github_transport_failure(tmp_path: Path) -> None:
    config = _config()
    adapter = Mock()
    adapter.remote_policy.side_effect = GitHubError("authentication failed")

    with (
        patch("converge_orchestrator.acceptance_cli.load_config", return_value=config),
        patch("converge_orchestrator.acceptance_cli._validate_acceptance_preconditions"),
        patch("converge_orchestrator.acceptance_cli.GitHubAdapter", return_value=adapter),
        pytest.raises(AcceptanceSupervisorError, match="remote CI preflight failed"),
    ):
        _acceptance_preflight(tmp_path / "converge.yaml")


def test_supervise_stops_before_controller_when_remote_preflight_fails(tmp_path: Path) -> None:
    config_path = tmp_path / "converge.yaml"
    config_path.write_text("version: 1\n", encoding="utf-8")
    supervisor = Mock()

    with (
        patch(
            "converge_orchestrator.acceptance_cli._acceptance_preflight",
            side_effect=AcceptanceSupervisorError("required CI missing"),
        ),
        patch(
            "converge_orchestrator.acceptance_cli.supervise_external_acceptance",
            supervisor,
        ),
        pytest.raises(typer.BadParameter, match="required CI missing"),
    ):
        supervise(
            config=config_path,
            project_id="external-acceptance",
            expected_risk_flag="forbidden_public_api_change",
            output=tmp_path / "evidence.json",
            poll_seconds=1.0,
        )

    supervisor.assert_not_called()


def test_acceptance_cli_click_app_builds_cleanly() -> None:
    """Building the click app must succeed for every registered command.

    Typer constructs each command's parameters lazily at invocation time, so a malformed option
    (for example a tuple ``help=`` from a stray trailing comma) only surfaces when the CLI is
    actually invoked. ``--help`` on the group forces every command's parameters to be built.
    """

    result = CliRunner().invoke(app, ["--help"])

    assert result.exit_code == 0
    assert "supervise" in result.output
