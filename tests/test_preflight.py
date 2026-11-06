"""RED tests for REQ-REPO-002: bootstrap repository preflight.

REQ-REPO-002 (CONVERGE_ORCHESTRATOR_2_0_SOURCE_OF_TRUTH.md:L278,
[9. Repository modes, greenfield bootstrap, and brownfield discovery]) requires bootstrap
preflight to establish repository existence, local Git identity when present, configured
base-branch state, working-tree cleanliness when applicable, remote identity when
configured, and non-overlap of repository, state, Source-of-Truth, secret, and active
controller authority paths.

The deterministic bootstrap repository preflight
``converge_orchestrator.preflight.repository_preflight`` does not exist yet, so every
test below fails with the literal RED marker
``repository preflight must reject overlapping authority paths`` until the preflight is
implemented and wired to fail closed on every listed violation.
"""

from __future__ import annotations

import importlib
import os
import re
import subprocess
from pathlib import Path
from typing import Any

import pytest

RED_MARKER = "repository preflight must reject overlapping authority paths"

_preflight_module: Any | None = None
try:
    _preflight_module = importlib.import_module("converge_orchestrator.preflight")
except ImportError:
    pass

PreflightError: type[BaseException] = Exception
if _preflight_module is not None:
    PreflightError = getattr(_preflight_module, "PreflightError", Exception)


def _empty_git_config(path: Path) -> Path:
    """Return an empty Git configuration file to isolate the test repository."""
    config = path / ".gitconfig_isolated"
    config.write_text("", encoding="utf-8")
    return config


def _git_env(path: Path) -> dict[str, str]:
    """Build an environment that ignores the caller's Git identity and global config."""
    empty = _empty_git_config(path)
    return {
        **os.environ,
        "HOME": str(path / "fakehome"),
        "GIT_CONFIG_GLOBAL": str(empty),
        "GIT_CONFIG_SYSTEM": str(empty),
    }


def _init_git_repo(
    path: Path,
    *,
    identity: bool = True,
    remote: bool = True,
    base_branch: str = "main",
) -> Path:
    """Create a minimal, isolated Git repository for deterministic preflight tests."""
    path.mkdir(parents=True, exist_ok=True)
    env = _git_env(path)
    subprocess.run(
        ["git", "init", "--initial-branch", base_branch],
        cwd=path,
        env=env,
        check=True,
        capture_output=True,
    )
    if identity:
        subprocess.run(
            ["git", "config", "user.name", "Converge Test"],
            cwd=path,
            env=env,
            check=True,
            capture_output=True,
        )
        subprocess.run(
            ["git", "config", "user.email", "test@example.com"],
            cwd=path,
            env=env,
            check=True,
            capture_output=True,
        )
    if remote:
        subprocess.run(
            ["git", "remote", "add", "origin", "https://example.com/repo.git"],
            cwd=path,
            env=env,
            check=True,
            capture_output=True,
        )
    (path / "README.md").write_text("# repo\n", encoding="utf-8")
    subprocess.run(
        ["git", "add", "README.md"],
        cwd=path,
        env=env,
        check=True,
        capture_output=True,
    )
    subprocess.run(
        [
            "git",
            "-c", "user.name=Converge Test",
            "-c", "user.email=test@example.com",
            "commit", "-m", "init",
        ],
        cwd=path,
        env=env,
        check=True,
        capture_output=True,
    )
    return path


def _preflight(repo: Path, **kwargs: Any) -> Any:
    """Invoke the REQ-REPO-002 repository preflight, failing closed if it is missing.

    ``converge_orchestrator.preflight.repository_preflight`` is the not-yet-existing
    deterministic preflight that establishes git identity, base-branch state, remote
    configuration, working-tree cleanliness, and authority-path non-overlap.  Until it
    exists, this helper raises the literal RED marker so the tests fail closed.
    """
    if _preflight_module is None:
        pytest.fail(RED_MARKER)
    return _preflight_module.repository_preflight(repo, **kwargs)


def test_preflight_records_identity_when_present(tmp_path: Path) -> None:
    repo = _init_git_repo(tmp_path / "repo", identity=True, remote=False)
    result = _preflight(repo)
    assert result.identity == ("Converge Test", "test@example.com")


def test_preflight_records_identity_absent(tmp_path: Path) -> None:
    repo = _init_git_repo(tmp_path / "repo", identity=False, remote=False)
    result = _preflight(repo)
    assert result.identity is None


def test_preflight_records_base_branch_valid(tmp_path: Path) -> None:
    repo = _init_git_repo(tmp_path / "repo", base_branch="main")
    result = _preflight(repo, base_branch="main")
    assert result.base_branch_exists is True


def test_preflight_records_remote_configured(tmp_path: Path) -> None:
    repo = _init_git_repo(tmp_path / "repo", remote=True)
    result = _preflight(repo)
    assert result.remote_configured is True


def test_preflight_records_clean_tree(tmp_path: Path) -> None:
    repo = _init_git_repo(tmp_path / "repo")
    result = _preflight(repo)
    assert result.clean is True


@pytest.mark.parametrize(
    "child_key,parent_key",
    [
        ("state_path", "repository_path"),
        ("source_of_truth_path", "repository_path"),
        ("secret_path", "repository_path"),
        ("controller_authority_path", "repository_path"),
        ("source_of_truth_path", "state_path"),
        ("secret_path", "state_path"),
        ("controller_authority_path", "state_path"),
        ("secret_path", "source_of_truth_path"),
        ("controller_authority_path", "source_of_truth_path"),
        ("controller_authority_path", "secret_path"),
    ],
)
def test_preflight_rejects_overlapping_authority_paths(
    tmp_path: Path,
    child_key: str,
    parent_key: str,
) -> None:
    clause = f"{child_key}_overlaps_{parent_key}"
    repo = _init_git_repo(tmp_path / "repo", identity=False, remote=False)
    paths: dict[str, Path] = {
        "repository_path": repo,
        "state_path": tmp_path / "state",
        "source_of_truth_path": tmp_path / "sot.md",
        "secret_path": tmp_path / "secrets",
        "controller_authority_path": tmp_path / "controller",
    }
    # Make the two authority paths overlap by making them identical.
    paths[child_key] = paths[parent_key]

    with pytest.raises(PreflightError, match=re.escape(clause)):
        _preflight(
            paths["repository_path"],
            state_path=paths["state_path"],
            source_of_truth_path=paths["source_of_truth_path"],
            secret_path=paths["secret_path"],
            controller_authority_path=paths["controller_authority_path"],
        )
