from __future__ import annotations

import re
import shutil
from pathlib import Path

from .shell import run


class GitError(RuntimeError):
    pass


def _git(repo: Path, *args: str, timeout: int = 300) -> str:
    result = run(["git", *args], cwd=repo, timeout=timeout)
    if result.returncode != 0:
        raise GitError(result.stdout)
    return result.stdout.strip()


def ensure_clean(repo: Path) -> None:
    if _git(repo, "status", "--porcelain"):
        raise GitError("Repository must be clean before orchestration starts.")


def update_base(repo: Path, branch: str) -> None:
    _git(repo, "fetch", "origin")
    _git(repo, "checkout", branch)
    _git(repo, "pull", "--ff-only", "origin", branch)


def create_worktree(repo: Path, worktree_root: Path, task_id: str, base_branch: str) -> tuple[Path, str]:
    safe = re.sub(r"[^a-zA-Z0-9._-]+", "-", task_id).strip("-").lower()
    branch = f"converge/{safe}"
    target = worktree_root / safe
    if target.exists():
        shutil.rmtree(target)
    run(["git", "branch", "-D", branch], cwd=repo, timeout=60)
    _git(repo, "worktree", "add", "-b", branch, str(target), f"origin/{base_branch}")
    return target, branch


def diff(worktree: Path, base_branch: str) -> str:
    return _git(worktree, "diff", f"origin/{base_branch}...HEAD") + "\n" + _git(worktree, "diff")


def commit_all(worktree: Path, message: str) -> str | None:
    if not _git(worktree, "status", "--porcelain"):
        return None
    _git(worktree, "add", "-A")
    _git(worktree, "commit", "-m", message)
    return _git(worktree, "rev-parse", "HEAD")


def push(worktree: Path, branch: str) -> None:
    _git(worktree, "push", "-u", "origin", branch, timeout=900)
