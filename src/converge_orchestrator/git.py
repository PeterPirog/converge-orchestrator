from __future__ import annotations

import fnmatch
import re
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


def current_head(repo: Path) -> str:
    return _git(repo, "rev-parse", "HEAD")


def update_base(repo: Path, branch: str) -> str:
    _git(repo, "fetch", "origin")
    _git(repo, "checkout", branch)
    _git(repo, "pull", "--ff-only", "origin", branch)
    return current_head(repo)


def cleanup_worktree(repo: Path, target: Path, branch: str) -> None:
    listed = _git(repo, "worktree", "list", "--porcelain")
    if str(target) in listed:
        _git(repo, "worktree", "remove", "--force", str(target))
    if target.exists():
        raise GitError(f"Unable to clean stale worktree: {target}")
    result = run(["git", "branch", "-D", branch], cwd=repo, timeout=60)
    if result.returncode not in (0, 1):
        raise GitError(result.stdout)


def create_worktree(
    repo: Path,
    worktree_root: Path,
    task_id: str,
    base_branch: str,
    branch_prefix: str = "converge/",
) -> tuple[Path, str]:
    safe = re.sub(r"[^a-zA-Z0-9._-]+", "-", task_id).strip("-").lower()
    branch = f"{branch_prefix}{safe}"
    target = worktree_root / safe
    cleanup_worktree(repo, target, branch)
    _git(repo, "worktree", "add", "-b", branch, str(target), f"origin/{base_branch}")
    return target, branch


def diff(worktree: Path, base_branch: str) -> str:
    committed = _git(worktree, "diff", f"origin/{base_branch}...HEAD")
    working = _git(worktree, "diff", "HEAD")
    return f"{committed}\n{working}".strip()


def changed_files(worktree: Path, base_branch: str) -> list[str]:
    names: set[str] = set()
    for args in (
        ("diff", "--name-only", f"origin/{base_branch}...HEAD"),
        ("diff", "--name-only", "HEAD"),
    ):
        output = _git(worktree, *args)
        names.update(line for line in output.splitlines() if line)
    status = _git(worktree, "status", "--porcelain")
    for line in status.splitlines():
        path = line[3:].split(" -> ")[-1].strip()
        if path:
            names.add(path)
    return sorted(names)


def diff_line_count(worktree: Path, base_branch: str) -> int:
    output = _git(worktree, "diff", "--numstat", f"origin/{base_branch}")
    total = 0
    tracked: set[str] = set()
    for line in output.splitlines():
        parts = line.split("\t")
        if len(parts) != 3:
            continue
        added, deleted, path = parts
        tracked.add(path)
        if added.isdigit():
            total += int(added)
        if deleted.isdigit():
            total += int(deleted)
    status = _git(worktree, "status", "--porcelain")
    for line in status.splitlines():
        if not line.startswith("?? "):
            continue
        path = line[3:].strip()
        if path in tracked:
            continue
        candidate = worktree / path
        if candidate.is_file():
            try:
                total += len(candidate.read_text(encoding="utf-8").splitlines())
            except UnicodeDecodeError:
                total += 1
    return total


def paths_within_allowlist(paths: list[str], patterns: list[str]) -> bool:
    if not patterns:
        return True
    return all(any(fnmatch.fnmatch(path, pattern) for pattern in patterns) for path in paths)


def delete_remote_branch(repo: Path, branch: str) -> None:
    run(["git", "push", "origin", "--delete", branch], cwd=repo, timeout=300)


def commit_all(worktree: Path, message: str) -> str | None:
    if not _git(worktree, "status", "--porcelain"):
        return None
    _git(worktree, "add", "-A")
    _git(worktree, "commit", "-m", message)
    return current_head(worktree)


def push(worktree: Path, branch: str) -> None:
    _git(worktree, "push", "-u", "origin", branch, timeout=900)
