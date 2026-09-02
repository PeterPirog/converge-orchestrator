from __future__ import annotations

import shlex
import subprocess
from pathlib import Path


def run(
    command: list[str],
    cwd: Path,
    timeout: int = 1800,
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        command,
        cwd=cwd,
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
        shell=shell,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        timeout=timeout,
        check=False,
    )
