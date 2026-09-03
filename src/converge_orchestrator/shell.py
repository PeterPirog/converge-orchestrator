from __future__ import annotations

import os
import shlex
import subprocess
from pathlib import Path


def _merged_env(env: dict[str, str] | None) -> dict[str, str] | None:
    if env is None:
        return None
    merged = os.environ.copy()
    merged.update(env)
    return merged


def run(
    command: list[str],
    cwd: Path,
    timeout: int = 1800,
    *,
    env: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        command,
        cwd=cwd,
        env=_merged_env(env),
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        timeout=timeout,
        check=False,
    )


def run_configured(
    command: str | list[str],
    cwd: Path,
    timeout: int = 1800,
    *,
    shell: bool = False,
    env: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    argv: str | list[str]
    if isinstance(command, list):
        argv = command
    elif shell:
        argv = command
    else:
        argv = shlex.split(command)
    return subprocess.run(
        argv,
        cwd=cwd,
        env=_merged_env(env),
        shell=shell,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        timeout=timeout,
        check=False,
    )
