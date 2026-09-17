from __future__ import annotations

# Subprocess output is always decoded as UTF-8 with errors="replace". The implicit locale codec
# (cp1250/cp1252 on many Windows hosts) crashes subprocess.run with UnicodeDecodeError when a
# child emits UTF-8 bytes that are undefined in the locale code page (external acceptance V20:
# a single 0x88 byte in ~91KB of agent output killed a controller reader thread and froze the
# whole run). Replaced code points keep evidence readable and never abort execution.
import os
import shlex
import subprocess
from pathlib import Path


def _merged_env(
    env: dict[str, str] | None,
    *,
    inherit_env: bool = True,
) -> dict[str, str] | None:
    if env is None and inherit_env:
        return None
    merged = os.environ.copy() if inherit_env else {}
    if env:
        merged.update(env)
    return merged


def run(
    command: list[str],
    cwd: Path,
    timeout: int = 1800,
    *,
    env: dict[str, str] | None = None,
    inherit_env: bool = True,
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        command,
        cwd=cwd,
        env=_merged_env(env, inherit_env=inherit_env),
        encoding="utf-8",
        errors="replace",
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
    inherit_env: bool = True,
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
        env=_merged_env(env, inherit_env=inherit_env),
        shell=shell,
        encoding="utf-8",
        errors="replace",
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        timeout=timeout,
        check=False,
    )
