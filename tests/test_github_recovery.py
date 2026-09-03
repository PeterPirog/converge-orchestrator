from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import Mock, patch

from converge_orchestrator.github import GitHubAdapter
from converge_orchestrator.models import ProjectConfig, PullRequestInfo


def _config(tmp_path: Path) -> ProjectConfig:
    return ProjectConfig(
        repo_path=tmp_path,
        requirements_path=tmp_path / "architecture.md",
        github_repo="owner/repo",
        agents={},
    )


def _pr() -> PullRequestInfo:
    return PullRequestInfo(
        number=17,
        url="https://github.com/owner/repo/pull/17",
        head_sha="candidate-sha",
        state="open",
    )


def test_ensure_pull_request_reuses_existing_pr_after_checkpoint_race(
    tmp_path: Path,
) -> None:
    adapter = GitHubAdapter(_config(tmp_path))
    with (
        patch.object(adapter, "find_open_pull_request", return_value=_pr()),
        patch.object(adapter, "create_pull_request") as create,
    ):
        result = adapter.ensure_pull_request(
            head="converge/arch-001-1",
            base="main",
            title="candidate",
            body="body",
        )

    assert result.number == 17
    create.assert_not_called()


def test_find_open_pull_request_queries_exact_owner_branch_and_base(tmp_path: Path) -> None:
    adapter = GitHubAdapter(_config(tmp_path))
    payload = [
        {
            "number": 17,
            "html_url": "https://github.com/owner/repo/pull/17",
            "head": {"sha": "candidate-sha"},
            "state": "open",
        }
    ]
    with patch.object(adapter, "_gh", return_value=json.dumps(payload)) as gh:
        result = adapter.find_open_pull_request(
            head="converge/arch-001-1",
            base="main",
        )

    assert result is not None
    assert result.number == 17
    args = gh.call_args.args[0]
    assert args[0] == "api"
    assert "state=open" in args[1]
    assert "head=owner%3Aconverge%2Farch-001-1" in args[1]
    assert "base=main" in args[1]


def test_merge_returns_existing_merge_sha_when_langgraph_retries_node(
    tmp_path: Path,
) -> None:
    adapter = GitHubAdapter(_config(tmp_path))
    response = {
        "merged": True,
        "merge_commit_sha": "already-merged-sha",
    }
    api = Mock(return_value=response)

    with patch.object(adapter, "_api_json", api):
        merged = adapter.merge(17)

    assert merged == "already-merged-sha"
    api.assert_called_once_with("repos/owner/repo/pulls/17")
