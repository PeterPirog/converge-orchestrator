from __future__ import annotations

import subprocess
from pathlib import Path
from unittest.mock import patch

import pytest

from converge_orchestrator.remote import (
    RemoteValidationError,
    repository_slug_from_remote,
    validate_origin_repository,
)


@pytest.mark.parametrize(
    ("remote", "expected"),
    [
        ("https://github.com/Owner/Repo.git", "Owner/Repo"),
        ("ssh://git@github.com/Owner/Repo.git", "Owner/Repo"),
        ("git@github.com:Owner/Repo.git", "Owner/Repo"),
    ],
)
def test_repository_slug_from_supported_github_remotes(
    remote: str,
    expected: str,
) -> None:
    assert repository_slug_from_remote(remote) == expected


def test_repository_slug_rejects_non_github_host() -> None:
    with pytest.raises(RemoteValidationError, match="expected github.com"):
        repository_slug_from_remote("https://example.test/Owner/Repo.git")


def test_repository_slug_rejects_ambiguous_path() -> None:
    with pytest.raises(RemoteValidationError, match="owner/repo pair"):
        repository_slug_from_remote("https://github.com/group/subgroup/repo.git")


def test_validate_origin_accepts_case_insensitive_owner_repo(tmp_path: Path) -> None:
    result = subprocess.CompletedProcess(
        args=["git"],
        returncode=0,
        stdout="git@github.com:Owner/Repo.git\n",
    )
    with patch("converge_orchestrator.remote.run", return_value=result):
        assert validate_origin_repository(tmp_path, "owner/repo") == "Owner/Repo"


def test_validate_origin_rejects_different_repository(tmp_path: Path) -> None:
    result = subprocess.CompletedProcess(
        args=["git"],
        returncode=0,
        stdout="https://github.com/owner/other.git\n",
    )
    with patch("converge_orchestrator.remote.run", return_value=result):
        with pytest.raises(RemoteValidationError, match="Git origin mismatch"):
            validate_origin_repository(tmp_path, "owner/repo")


def test_validate_origin_fails_when_git_cannot_resolve_origin(tmp_path: Path) -> None:
    result = subprocess.CompletedProcess(
        args=["git"],
        returncode=2,
        stdout="No such remote 'origin'\n",
    )
    with patch("converge_orchestrator.remote.run", return_value=result):
        with pytest.raises(RemoteValidationError, match="Unable to resolve git origin"):
            validate_origin_repository(tmp_path, "owner/repo")
