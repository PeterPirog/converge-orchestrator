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


def _worktree_entries(repo: Path) -> dict[Path, str | None]:
    output = _git(repo, "worktree", "list", "--porcelain")
    entries: dict[Path, str | None] = {}
    target: Path | None = None
    branch: str | None = None

    def flush() -> None:
        nonlocal target, branch
        if target is not None:
            entries[target] = branch
        target = None
        branch = None

    for line in [*output.splitlines(), ""]:
        if not line:
            flush()
            continue
        if line.startswith("worktree "):
            flush()
            target = Path(line.removeprefix("worktree ")).resolve()
        elif line.startswith("branch refs/heads/"):
            branch = line.removeprefix("branch refs/heads/")
    return entries


def _local_branch_exists(repo: Path, branch: str) -> bool:
    result = run(
        ["git", "show-ref", "--verify", "--quiet", f"refs/heads/{branch}"],
        cwd=repo,
        timeout=60,
    )
    if result.returncode not in (0, 1):
        raise GitError(result.stdout)
    return result.returncode == 0


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
    """Create or safely adopt the deterministic worktree for a LangGraph task.

    LangGraph checkpoints provide at-least-once node execution after a process crash. This function
    therefore never destroys an existing candidate as part of creation. A matching registered
    worktree is adopted, a preserved local task branch is reattached, and any ambiguous filesystem
    state fails closed for explicit recovery instead of being force-deleted.
    """
    safe = re.sub(r"[^a-zA-Z0-9._-]+", "-", task_id).strip("-").lower()
    branch = f"{branch_prefix}{safe}"
    root = worktree_root.expanduser().resolve()
    target = (root / safe).resolve()
    if target.parent != root:
        raise GitError(f"Unsafe worktree target outside configured root: {target}")

    entries = _worktree_entries(repo)
    registered_branch = entries.get(target)
    if target.exists() or target in entries:
        if registered_branch is None:
            raise GitError(
                f"Existing worktree path is not a registered branch worktree: {target}"
            )
        if registered_branch != branch:
            raise GitError(
                f"Existing worktree {target} belongs to {registered_branch}, expected {branch}"
            )
        if not target.exists():
            raise GitError(f"Git registers worktree but its path is missing: {target}")
        return target, branch

    root.mkdir(parents=True, exist_ok=True)
    if _local_branch_exists(repo, branch):
        _git(repo, "worktree", "add", str(target), branch)
    else:
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


def existing_candidate_commit(worktree: Path, base_branch: str) -> str | None:
    """Return an already-created candidate commit after a checkpoint race/crash."""
    if _git(worktree, "status", "--porcelain"):
        return None
    ahead = _git(worktree, "rev-list", "--count", f"origin/{base_branch}..HEAD")
    try:
        count = int(ahead)
    except ValueError as exc:
        raise GitError(f"Invalid git rev-list count: {ahead}") from exc
    return current_head(worktree) if count > 0 else None


def push(worktree: Path, branch: str) -> None:
    _git(worktree, "push", "-u", "origin", branch, timeout=900)
