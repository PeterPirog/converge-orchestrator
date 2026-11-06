"""Reverse-direction overlap tests for REQ-REPO-002."""
from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

from converge_orchestrator.preflight import _OVERLAP_PAIRS, PreflightError, repository_preflight


def _init(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    cfg = path.parent / ".gitconfig_isolated"
    cfg.write_text("", encoding="utf-8")
    env = {**os.environ, "GIT_CONFIG_GLOBAL": str(cfg), "GIT_CONFIG_SYSTEM": str(cfg)}
    subprocess.run(["git", "init", "--initial-branch", "main"], cwd=path, env=env, check=True)
    subprocess.run(["git", "config", "user.name", "Test"], cwd=path, env=env, check=True)
    subprocess.run(["git", "config", "user.email", "t@e.com"], cwd=path, env=env, check=True)
    subprocess.run(["git", "commit", "--allow-empty", "-m", "init"], cwd=path, env=env, check=True)
    return path


@pytest.mark.parametrize("a, b", _OVERLAP_PAIRS)
def test_preflight_rejects_reverse_nested_overlap(tmp_path: Path, a: str, b: str) -> None:
    repo = _init(tmp_path / "repo")
    kwargs: dict[str, Path] = {"repository_path": repo}
    if b == "repository_path":
        parent = tmp_path / "parent"
        kwargs[a] = parent
        kwargs[b] = _init(parent / "repo")
    else:
        root = tmp_path / a
        root.mkdir(parents=True)
        kwargs[a] = root
        kwargs[b] = root / "nested"
    with pytest.raises(PreflightError, match=f"{b}_overlaps_{a}"):
        repository_preflight(repository_path=kwargs.pop("repository_path"), **kwargs)
