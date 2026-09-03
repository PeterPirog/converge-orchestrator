from __future__ import annotations

from pathlib import Path

import pytest

from converge_orchestrator.ci_flakes import (
    FlakyCIPolicy,
    GitHubFlakyCIAdapter,
    _actions_job_id,
    choose_flaky_retry,
    flaky_ci_policy_from_mapping,
)
from converge_orchestrator.models import CIResult, ProjectConfig


def _result(*checks: dict) -> CIResult:
    return CIResult(
        status="fail",
        head_sha="abc",
        checks=[
            {
                "kind": "remote_policy",
                "authoritative": True,
                "required_checks": [{"context": "CI", "app_id": None}],
            },
            *checks,
        ],
    )


def _failed_check(name: str, *, required: bool = True) -> dict:
    return {
        "kind": "check_run",
        "name": name,
        "status": "completed",
        "conclusion": "failure",
        "required": required,
    }


def test_flaky_policy_requires_exact_unique_names() -> None:
    policy = flaky_ci_policy_from_mapping(
        {
            "github": {
                "flaky_ci": {
                    "checks": [" test (3.13) "],
                    "max_retries_per_check": 1,
                }
            }
        }
    )
    assert policy.checks == ["test (3.13)"]
    assert policy.enabled

    with pytest.raises(ValueError, match="duplicates"):
        flaky_ci_policy_from_mapping(
            {
                "github": {
                    "flaky_ci": {
                        "checks": ["CI", "CI"],
                        "max_retries_per_check": 1,
                    }
                }
            }
        )


def test_only_explicit_authoritative_flaky_failure_is_retryable() -> None:
    policy = FlakyCIPolicy(checks=["CI"], max_retries_per_check=1)
    result = _result(
        _failed_check("CI"),
        _failed_check("unrelated", required=False),
    )

    assert choose_flaky_retry(result, policy, {}) == "CI"


def test_mixed_flaky_and_nonflaky_required_failures_are_not_retried() -> None:
    policy = FlakyCIPolicy(checks=["CI"], max_retries_per_check=1)
    result = _result(_failed_check("CI"), _failed_check("security"))

    assert choose_flaky_retry(result, policy, {}) is None


def test_commit_status_failure_is_never_classified_as_flaky_job() -> None:
    policy = FlakyCIPolicy(checks=["CI"], max_retries_per_check=1)
    result = _result(
        {
            "kind": "status",
            "name": "CI",
            "status": "failure",
            "required": True,
        }
    )

    assert choose_flaky_retry(result, policy, {}) is None


def test_retry_budget_is_strict() -> None:
    policy = FlakyCIPolicy(checks=["CI"], max_retries_per_check=1)
    result = _result(_failed_check("CI"))

    assert choose_flaky_retry(result, policy, {"CI": 1}) is None


def test_actions_job_id_requires_canonical_same_repo_url() -> None:
    assert (
        _actions_job_id(
            "https://github.com/owner/repo/actions/runs/123/job/456",
            "owner/repo",
        )
        == 456
    )
    assert (
        _actions_job_id(
            "https://github.com/other/repo/actions/runs/123/job/456",
            "owner/repo",
        )
        is None
    )
    assert _actions_job_id("https://example.test/actions/runs/123/job/456", "owner/repo") is None


class _Adapter(GitHubFlakyCIAdapter):
    def __init__(self, config: ProjectConfig):
        super().__init__(config)
        self.commands: list[list[str]] = []

    def _api_json(self, endpoint: str, **kwargs):  # type: ignore[no-untyped-def]
        assert endpoint == "repos/owner/repo/commits/abc/check-runs"
        return {
            "check_runs": [
                {
                    "name": "CI",
                    "status": "completed",
                    "conclusion": "failure",
                    "details_url": "https://github.com/owner/repo/actions/runs/123/job/456",
                }
            ]
        }

    def _gh(self, args, **kwargs):  # type: ignore[no-untyped-def]
        del kwargs
        self.commands.append(args)
        return ""


def test_actions_rerun_uses_exact_job_endpoint(tmp_path: Path) -> None:
    cfg = ProjectConfig(
        repo_path=tmp_path,
        requirements_path=tmp_path / "architecture.md",
        github_repo="owner/repo",
        agents={},
    )
    adapter = _Adapter(cfg)

    assert adapter.rerun_failed_actions_check("abc", "CI") == 456
    assert adapter.commands == [
        ["api", "--method", "POST", "repos/owner/repo/actions/jobs/456/rerun"]
    ]
