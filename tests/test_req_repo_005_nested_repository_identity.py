"""REQ-REPO-005: nested Git repository identities remain separate.

A deterministic fail-closed local-only guard exists so that editing a path whose
ancestor chain crosses a nested Git boundary (nested .git directory, submodule
.git file, embedded linked-worktree .git pointer to a different repository, or
external mount carrying .git) inside the authorized candidate worktree raises a
WorkspaceAffinityError containing the literal marker
'REQ-REPO-005 nested repository authority denied' unless the project mapping
explicitly grants authority for that nested repository identity, while ordinary
primary-repository paths remain admitted.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from converge_orchestrator.workspace_identity import (
    WorkspaceAffinityError,
    mutation_domain_id,
    workspace_id,
)

# Literal TDD RED evidence marker (tdd.expected_failure_pattern). Carried by every
# failure below until the additive pure guard
# converge_orchestrator.workspace_identity.assert_nested_repository_authority
# lands. Its appearance in a test run is deterministic RED evidence.
_MARKER = "REQ-REPO-005 nested repository authority denied"


def _guard():
    """Return the guard, failing closed with the literal RED marker when absent."""
    try:
        from converge_orchestrator.workspace_identity import (
            assert_nested_repository_authority,
        )
    except ImportError as exc:
        raise AssertionError(_MARKER) from exc
    return assert_nested_repository_authority


def _run_git(cwd: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", *args],
        cwd=cwd,
        check=True,
        text=True,
        capture_output=True,
    )
    return result.stdout.strip()


def _git_repo(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    _run_git(path, "init", "-b", "main")
    _run_git(path, "config", "user.email", "converge@example.invalid")
    _run_git(path, "config", "user.name", "Converge Test")
    return path


def test_primary_repository_path_is_admitted(tmp_path: Path) -> None:
    repo = _git_repo(tmp_path / "repo")
    project = {"id": "test", "workspace_id": workspace_id(repo)}
    target = repo / "src" / "file.py"
    # No nested boundary; the guard must not raise.
    _guard()(project, repo, target)


def test_nested_git_directory_is_rejected_without_authority(tmp_path: Path) -> None:
    repo = _git_repo(tmp_path / "repo")
    nested = _git_repo(repo / "vendor" / "nested")
    project = {"id": "test", "workspace_id": workspace_id(repo)}
    target = nested / "file.py"
    with pytest.raises(WorkspaceAffinityError, match=_MARKER):
        _guard()(project, repo, target)


def test_git_file_pointer_to_different_repository_is_rejected_without_authority(
    tmp_path: Path,
) -> None:
    # A .git file pointing to another repository's gitdir (submodule or linked
    # worktree form) marks a separate repository identity inside the worktree.
    repo = _git_repo(tmp_path / "repo")
    external = _git_repo(tmp_path / "external")
    mount = repo / "vendor" / "external"
    mount.mkdir(parents=True)
    (mount / ".git").write_text(f"gitdir: {(external / '.git').as_posix()}\n", encoding="utf-8")
    project = {"id": "test", "workspace_id": workspace_id(repo)}
    target = mount / "file.txt"
    with pytest.raises(WorkspaceAffinityError, match=_MARKER):
        _guard()(project, repo, target)


def test_explicit_nested_repository_authority_admits_edits(tmp_path: Path) -> None:
    repo = _git_repo(tmp_path / "repo")
    nested = _git_repo(repo / "vendor" / "nested")
    nested_id = mutation_domain_id(nested)
    project = {
        "id": "test",
        "workspace_id": workspace_id(repo),
        "nested_repository_authority": {nested_id: True},
    }
    target = nested / "file.py"
    # Authority is explicitly granted for this nested identity.
    _guard()(project, repo, target)
