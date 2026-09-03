from __future__ import annotations

import re
from pathlib import Path
from urllib.parse import urlparse

from .shell import run


class RemoteValidationError(RuntimeError):
    pass


def repository_slug_from_remote(remote_url: str) -> str:
    """Extract canonical owner/repo from common GitHub HTTPS and SSH remotes."""
    raw = remote_url.strip()
    if not raw:
        raise RemoteValidationError("origin remote URL is empty")

    if "://" in raw:
        parsed = urlparse(raw)
        path = parsed.path
    else:
        scp_like = re.match(r"^[^@\s]+@[^:\s]+:(?P<path>.+)$", raw)
        if not scp_like:
            raise RemoteValidationError(f"Unsupported origin remote URL: {raw}")
        path = scp_like.group("path")

    normalized = path.strip("/")
    if normalized.endswith(".git"):
        normalized = normalized[:-4]
    parts = [part for part in normalized.split("/") if part]
    if len(parts) != 2:
        raise RemoteValidationError(
            f"Origin remote does not identify a GitHub owner/repo pair: {raw}"
        )
    return f"{parts[0]}/{parts[1]}"


def validate_origin_repository(repo_path: Path, expected_repo: str) -> str:
    """Fail closed when local origin does not match configured GitHub owner/repo."""
    expected = expected_repo.strip().strip("/")
    if expected.count("/") != 1:
        raise RemoteValidationError(f"Invalid configured github_repo: {expected_repo}")

    result = run(
        ["git", "remote", "get-url", "origin"],
        cwd=repo_path,
        timeout=60,
    )
    if result.returncode != 0:
        raise RemoteValidationError(
            f"Unable to resolve git origin for {repo_path}: {result.stdout.strip()}"
        )
    actual = repository_slug_from_remote(result.stdout)
    if actual.casefold() != expected.casefold():
        raise RemoteValidationError(
            f"Git origin mismatch: expected {expected}, found {actual}"
        )
    return actual
