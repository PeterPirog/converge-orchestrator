from pathlib import Path

from converge_orchestrator.github import GitHubAdapter, RemotePolicy
from converge_orchestrator.models import ProjectConfig


class StubGitHubAdapter(GitHubAdapter):
    def __init__(self, config: ProjectConfig, responses: dict[str, dict]):
        super().__init__(config)
        self.responses = responses

    def _api_json(self, endpoint: str, **kwargs):  # type: ignore[no-untyped-def]
        return self.responses[endpoint]


def _config(tmp_path: Path) -> ProjectConfig:
    return ProjectConfig(
        repo_path=tmp_path,
        requirements_path=tmp_path / "architecture.md",
        github_repo="owner/repo",
        agents={},
    )


def _unprotected_policy() -> RemotePolicy:
    return RemotePolicy(
        base_branch="main",
        protected=False,
        authoritative=True,
        source="test",
    )


def test_ci_status_is_pending_when_no_checks_exist(tmp_path: Path) -> None:
    adapter = StubGitHubAdapter(
        _config(tmp_path),
        {
            "repos/owner/repo/commits/abc/check-runs": {"check_runs": []},
            "repos/owner/repo/commits/abc/status": {"statuses": []},
        },
    )
    assert adapter.ci_status("abc", _unprotected_policy()).status == "pending"


def test_ci_status_fails_on_failed_check(tmp_path: Path) -> None:
    adapter = StubGitHubAdapter(
        _config(tmp_path),
        {
            "repos/owner/repo/commits/abc/check-runs": {
                "check_runs": [
                    {"name": "CI", "status": "completed", "conclusion": "failure"}
                ]
            },
            "repos/owner/repo/commits/abc/status": {"statuses": []},
        },
    )
    assert adapter.ci_status("abc", _unprotected_policy()).status == "fail"
