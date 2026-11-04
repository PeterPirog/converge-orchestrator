"""Completeness of the review-lane candidate snapshot.

REQ-AGENT-016 requires every configured review lane to inspect a complete
representation of the publishable candidate snapshot. The candidate fingerprint
and review prompts are derived from converge_orchestrator.git.diff(). If diff()
omits untracked candidate files, lanes inspect an incomplete snapshot. This test
pins that untracked non-cache candidate files appear in the deterministic diff
output, while deterministic cache artifacts are never represented as candidate
content.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

from converge_orchestrator.git import diff

_CANDIDATE_SNAPSHOT_INCOMPLETE_MARKER = "AGENT016_CANDIDATE_SNAPSHOT_INCOMPLETE_UNTRACKED"


def _run_git(cwd: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True)


def _git_stdout(cwd: Path, *args: str) -> str:
    result = subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True, check=True)
    return result.stdout


def _repository(tmp_path: Path) -> Path:
    origin = tmp_path / "origin.git"
    subprocess.run(
        ["git", "init", "--bare", "--initial-branch=main", str(origin)],
        check=True,
        capture_output=True,
        text=True,
    )
    repo = tmp_path / "repo"
    subprocess.run(
        ["git", "clone", str(origin), str(repo)],
        check=True,
        capture_output=True,
        text=True,
    )
    _run_git(repo, "config", "user.email", "converge@example.invalid")
    _run_git(repo, "config", "user.name", "Converge Test")
    (repo / "README.md").write_text("baseline\n", encoding="utf-8")
    _run_git(repo, "add", ".")
    _run_git(repo, "commit", "-m", "baseline")
    _run_git(repo, "push", "-u", "origin", "main")
    return repo


def test_diff_includes_untracked_candidate_files_and_excludes_cache_artifacts(
    tmp_path: Path,
) -> None:
    """diff() must expose untracked candidate content; cache artifacts are invisible."""
    repo = _repository(tmp_path)

    # Real tracked content change so the diff is non-empty even before this fix.
    (repo / "README.md").write_text("baseline\nnew tracked line\n", encoding="utf-8")

    # Untracked candidate file that must be visible to every review lane.
    (repo / "src").mkdir()
    (repo / "src" / "untracked_feature.py").write_text(
        "def untracked():\n    return 'candidate'\n",
        encoding="utf-8",
    )

    # Deterministic cache artifact that must never be represented as candidate content.
    (repo / "src" / "__pycache__").mkdir()
    (repo / "src" / "__pycache__" / "mod.cpython-313.pyc").write_bytes(b"\x00compiled")

    patch = diff(repo, "main")

    # Tracked change is present.
    assert "new tracked line" in patch

    # Untracked candidate content is present so lanes see a complete snapshot.
    assert "untracked_feature.py" in patch, _CANDIDATE_SNAPSHOT_INCOMPLETE_MARKER
    assert "def untracked():" in patch, _CANDIDATE_SNAPSHOT_INCOMPLETE_MARKER
    assert "return 'candidate'" in patch, _CANDIDATE_SNAPSHOT_INCOMPLETE_MARKER

    # The failure marker itself must never leak into the deterministic diff text.
    assert _CANDIDATE_SNAPSHOT_INCOMPLETE_MARKER not in patch

    # Cache artifacts are never part of the publishable candidate snapshot.
    assert "__pycache__" not in patch
    assert "mod.cpython-313.pyc" not in patch
