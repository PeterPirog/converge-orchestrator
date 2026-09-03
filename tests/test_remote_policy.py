from __future__ import annotations

from pathlib import Path

from converge_orchestrator.github import (
    GitHubAdapter,
    GitHubError,
    RemotePolicy,
    RequiredCheck,
)
from converge_orchestrator.models import ProjectConfig

_RULES_ENDPOINT = "repos/owner/repo/rules/branches/main?per_page=100"


class StubGitHubAdapter(GitHubAdapter):
    def __init__(self, config: ProjectConfig, responses: dict[str, object]):
        super().__init__(config)
        self.responses = responses
        self.calls: list[str] = []

    def _api_json(self, endpoint: str, **kwargs):  # type: ignore[no-untyped-def]
        self.calls.append(endpoint)
        response = self.responses.get(endpoint)
        if isinstance(response, Exception):
            raise response
        if response is None:
            raise GitHubError(f"missing stub response: {endpoint}")
        assert isinstance(response, dict)
        return response

    def _api_paginated_list(  # type: ignore[no-untyped-def]
        self,
        endpoint: str,
        **kwargs,
    ):
        self.calls.append(endpoint)
        response = self.responses.get(endpoint, [])
        if isinstance(response, Exception):
            raise response
        assert isinstance(response, list)
        return response


def _config(tmp_path: Path) -> ProjectConfig:
    return ProjectConfig(
        repo_path=tmp_path,
        requirements_path=tmp_path / "architecture.md",
        github_repo="owner/repo",
        base_branch="main",
        agents={},
    )


def _required_policy(*checks: RequiredCheck) -> RemotePolicy:
    return RemotePolicy(
        base_branch="main",
        protected=True,
        authoritative=True,
        source="test",
        required_checks=tuple(checks),
    )


def _adapter(
    tmp_path: Path,
    *,
    checks: list[dict] | None = None,
    statuses: list[dict] | None = None,
) -> StubGitHubAdapter:
    return StubGitHubAdapter(
        _config(tmp_path),
        {
            "repos/owner/repo/commits/abc/check-runs": {
                "check_runs": checks or []
            },
            "repos/owner/repo/commits/abc/status": {
                "statuses": statuses or []
            },
        },
    )


def test_required_check_pass_ignores_unrelated_failure(tmp_path: Path) -> None:
    adapter = _adapter(
        tmp_path,
        checks=[
            {
                "name": "required-ci",
                "status": "completed",
                "conclusion": "success",
                "app": {"id": 42},
            },
            {
                "name": "optional-lint",
                "status": "completed",
                "conclusion": "failure",
                "app": {"id": 42},
            },
        ],
    )
    policy = _required_policy(RequiredCheck("required-ci", app_id=42))

    result = adapter.ci_status("abc", policy)

    assert result.status == "pass"
    optional = next(item for item in result.checks if item.get("name") == "optional-lint")
    assert optional["required"] is False


def test_missing_required_check_remains_pending(tmp_path: Path) -> None:
    adapter = _adapter(
        tmp_path,
        checks=[
            {
                "name": "optional-ci",
                "status": "completed",
                "conclusion": "success",
                "app": {"id": 42},
            }
        ],
    )
    policy = _required_policy(RequiredCheck("required-ci", app_id=42))

    assert adapter.ci_status("abc", policy).status == "pending"


def test_required_check_rejects_same_context_from_wrong_app(tmp_path: Path) -> None:
    adapter = _adapter(
        tmp_path,
        checks=[
            {
                "name": "required-ci",
                "status": "completed",
                "conclusion": "success",
                "app": {"id": 99},
            }
        ],
    )
    policy = _required_policy(RequiredCheck("required-ci", app_id=42))

    assert adapter.ci_status("abc", policy).status == "pending"


def test_required_commit_status_can_satisfy_context_without_app(tmp_path: Path) -> None:
    adapter = _adapter(
        tmp_path,
        statuses=[{"context": "legacy-ci", "state": "success"}],
    )
    policy = _required_policy(RequiredCheck("legacy-ci"))

    assert adapter.ci_status("abc", policy).status == "pass"


def test_required_failure_blocks_even_when_other_required_check_passes(
    tmp_path: Path,
) -> None:
    adapter = _adapter(
        tmp_path,
        checks=[
            {
                "name": "unit",
                "status": "completed",
                "conclusion": "success",
                "app": {"id": 7},
            },
            {
                "name": "security",
                "status": "completed",
                "conclusion": "failure",
                "app": {"id": 7},
            },
        ],
    )
    policy = _required_policy(
        RequiredCheck("unit", app_id=7),
        RequiredCheck("security", app_id=7),
    )

    assert adapter.ci_status("abc", policy).status == "fail"


def test_non_authoritative_protected_policy_never_passes(tmp_path: Path) -> None:
    adapter = _adapter(
        tmp_path,
        checks=[
            {
                "name": "CI",
                "status": "completed",
                "conclusion": "success",
            }
        ],
    )
    policy = RemotePolicy(
        base_branch="main",
        protected=True,
        authoritative=False,
        source="protected_policy_unavailable",
    )

    assert adapter.ci_status("abc", policy).status == "pending"


def test_remote_policy_uses_detailed_endpoint_when_summary_is_incomplete(
    tmp_path: Path,
) -> None:
    branch_endpoint = "repos/owner/repo/branches/main"
    detail_endpoint = (
        "repos/owner/repo/branches/main/protection/required_status_checks"
    )
    adapter = StubGitHubAdapter(
        _config(tmp_path),
        {
            branch_endpoint: {
                "protected": True,
                "protection": {
                    "required_status_checks": {"enforcement_level": "non_admins"}
                },
            },
            detail_endpoint: {
                "strict": True,
                "checks": [{"context": "CI", "app_id": 42}],
            },
        },
    )

    policy = adapter.remote_policy("main")

    assert policy.authoritative is True
    assert policy.source == "branch_protection"
    assert policy.required_checks == (RequiredCheck("CI", app_id=42),)
    assert detail_endpoint in adapter.calls
    assert _RULES_ENDPOINT in adapter.calls


def test_ruleset_only_status_checks_are_authoritative(tmp_path: Path) -> None:
    adapter = StubGitHubAdapter(
        _config(tmp_path),
        {
            "repos/owner/repo/branches/main": {
                "protected": True,
                "protection": {"required_status_checks": None},
            },
            _RULES_ENDPOINT: [
                {
                    "type": "required_status_checks",
                    "ruleset_source_type": "Organization",
                    "ruleset_id": 17,
                    "parameters": {
                        "required_status_checks": [
                            {"context": "CI", "integration_id": 42}
                        ],
                        "strict_required_status_checks_policy": True,
                    },
                }
            ],
        },
    )

    policy = adapter.remote_policy("main")

    assert policy.authoritative is True
    assert policy.source == "rulesets"
    assert policy.strict is True
    assert policy.required_checks == (RequiredCheck("CI", app_id=42),)


def test_classic_and_ruleset_status_checks_are_combined(tmp_path: Path) -> None:
    adapter = StubGitHubAdapter(
        _config(tmp_path),
        {
            "repos/owner/repo/branches/main": {
                "protected": True,
                "protection": {
                    "required_status_checks": {
                        "strict": False,
                        "checks": [{"context": "lint", "app_id": 7}],
                    }
                },
            },
            _RULES_ENDPOINT: [
                {
                    "type": "required_status_checks",
                    "parameters": {
                        "required_status_checks": [
                            {"context": "CI", "integration_id": 42},
                            {"context": "lint", "integration_id": 7},
                        ],
                        "strict_required_status_checks_policy": True,
                    },
                }
            ],
        },
    )

    policy = adapter.remote_policy("main")

    assert policy.source == "branch_protection+rulesets"
    assert policy.strict is True
    assert policy.required_checks == (
        RequiredCheck("lint", app_id=7),
        RequiredCheck("CI", app_id=42),
    )


def test_unreadable_effective_rules_are_fail_closed(tmp_path: Path) -> None:
    adapter = StubGitHubAdapter(
        _config(tmp_path),
        {
            "repos/owner/repo/branches/main": {
                "protected": True,
                "protection": {
                    "required_status_checks": {
                        "checks": [{"context": "CI", "app_id": 42}]
                    }
                },
            },
            _RULES_ENDPOINT: GitHubError("forbidden"),
        },
    )

    policy = adapter.remote_policy("main")

    assert policy.authoritative is False
    assert policy.source == "protected_policy_unavailable"


def test_malformed_ruleset_status_check_policy_is_fail_closed(tmp_path: Path) -> None:
    adapter = StubGitHubAdapter(
        _config(tmp_path),
        {
            "repos/owner/repo/branches/main": {
                "protected": True,
                "protection": {"required_status_checks": None},
            },
            _RULES_ENDPOINT: [
                {
                    "type": "required_status_checks",
                    "parameters": {"strict_required_status_checks_policy": True},
                }
            ],
        },
    )

    policy = adapter.remote_policy("main")

    assert policy.authoritative is False
    assert policy.source == "protected_policy_unavailable"


def test_protected_branch_with_unreadable_classic_policy_is_fail_closed(
    tmp_path: Path,
) -> None:
    branch_endpoint = "repos/owner/repo/branches/main"
    detail_endpoint = (
        "repos/owner/repo/branches/main/protection/required_status_checks"
    )
    adapter = StubGitHubAdapter(
        _config(tmp_path),
        {
            branch_endpoint: {"protected": True},
            detail_endpoint: GitHubError("forbidden"),
            _RULES_ENDPOINT: [],
        },
    )

    policy = adapter.remote_policy("main")

    assert policy.protected is True
    assert policy.authoritative is False
    assert policy.source == "protected_policy_unavailable"
