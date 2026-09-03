from __future__ import annotations

import os
import re
import shlex
import shutil
import subprocess
from pathlib import Path
from typing import Any, Literal

from .models import ProjectConfig
from .shell import run_configured

SandboxScope = Literal["agent", "quality"]
_ENV_REFERENCE = re.compile(r"\{env:([A-Za-z_][A-Za-z0-9_]*)\}")


def _env_references(value: Any) -> set[str]:
    found: set[str] = set()
    if isinstance(value, str):
        found.update(_ENV_REFERENCE.findall(value))
    elif isinstance(value, dict):
        for key, item in value.items():
            found.update(_env_references(key))
            found.update(_env_references(item))
    elif isinstance(value, (list, tuple, set)):
        for item in value:
            found.update(_env_references(item))
    return found


def _configured_env_names(config: ProjectConfig, scope: SandboxScope) -> set[str]:
    names = set(config.sandbox.pass_env)
    if scope != "agent":
        return names
    if config.model_gateway.api_key_env:
        names.add(config.model_gateway.api_key_env)
    names.update(_env_references(config.model_gateway.headers))
    names.update(_env_references(config.mcp))
    return names


def _mount(source: Path, *, readonly: bool) -> str:
    resolved = source.resolve()
    option = f"type=bind,src={resolved},dst={resolved}"
    return f"{option},readonly" if readonly else option


def _is_within(path: Path, parent: Path) -> bool:
    try:
        path.resolve().relative_to(parent.resolve())
    except ValueError:
        return False
    return True


def _inner_command(command: str | list[str], *, shell: bool) -> list[str]:
    if shell:
        rendered = shlex.join(command) if isinstance(command, list) else command
        return ["/bin/sh", "-lc", rendered]
    return list(command) if isinstance(command, list) else shlex.split(command)


class SandboxPreflightError(RuntimeError):
    pass


class ExecutionSandbox:
    """Run untrusted project/model processes with an optional container boundary."""

    def __init__(self, config: ProjectConfig):
        self.config = config

    def _validate_network_policy(self) -> None:
        policy = self.config.sandbox
        if (
            policy.mode == "container"
            and policy.require_internal_agent_network
            and policy.agent_network in {"none", "host"}
        ):
            raise SandboxPreflightError(
                "sandbox requires a named internal agent network when "
                "require_internal_agent_network=true"
            )

    def preflight(self) -> None:
        policy = self.config.sandbox
        if policy.mode == "host":
            return
        self._validate_network_policy()
        if shutil.which(policy.engine) is None:
            raise SandboxPreflightError(
                f"sandbox engine executable not found on PATH: {policy.engine}"
            )
        image = policy.image
        if not image:
            raise SandboxPreflightError("sandbox image is not configured")
        image_result = run_configured(
            [policy.engine, "image", "inspect", image],
            cwd=self.config.repo_path,
            timeout=30,
        )
        if image_result.returncode != 0:
            raise SandboxPreflightError(
                f"sandbox image is not available locally: {image}; "
                "Converge never pulls sandbox images implicitly"
            )
        networks = {policy.agent_network, policy.quality_network} - {"none", "host"}
        for network in sorted(networks):
            result = run_configured(
                [policy.engine, "network", "inspect", network],
                cwd=self.config.repo_path,
                timeout=30,
            )
            if result.returncode != 0:
                raise SandboxPreflightError(
                    f"configured sandbox network does not exist: {network}"
                )

    def run(
        self,
        command: str | list[str],
        *,
        cwd: Path,
        timeout: int = 1800,
        shell: bool = False,
        env: dict[str, str] | None = None,
        scope: SandboxScope = "quality",
        writable_cwd: bool = True,
        include_state: bool = False,
    ) -> subprocess.CompletedProcess[str]:
        if self.config.sandbox.mode == "host":
            return run_configured(
                command,
                cwd=cwd,
                timeout=timeout,
                shell=shell,
                env=env,
            )
        self._validate_network_policy()
        return self._run_container(
            command,
            cwd=cwd,
            timeout=timeout,
            shell=shell,
            env=env,
            scope=scope,
            writable_cwd=writable_cwd,
            include_state=include_state,
        )

    def _run_container(
        self,
        command: str | list[str],
        *,
        cwd: Path,
        timeout: int,
        shell: bool,
        env: dict[str, str] | None,
        scope: SandboxScope,
        writable_cwd: bool,
        include_state: bool,
    ) -> subprocess.CompletedProcess[str]:
        policy = self.config.sandbox
        image = policy.image
        if not image:
            raise SandboxPreflightError("sandbox image is not configured")

        cwd = cwd.resolve()
        process_env = os.environ.copy()
        if env:
            process_env.update(env)
        process_env["GIT_OPTIONAL_LOCKS"] = "0"
        process_env["HOME"] = "/tmp/converge-home"
        process_env["XDG_CACHE_HOME"] = "/tmp/converge-cache"

        network = policy.agent_network if scope == "agent" else policy.quality_network
        argv = [
            policy.engine,
            "run",
            "--rm",
            "--init",
            "--pull=never",
            "--entrypoint",
            "",
            "--cap-drop=ALL",
            "--security-opt",
            "no-new-privileges:true",
            "--pids-limit",
            str(policy.pids_limit),
            "--network",
            network,
            "--workdir",
            str(cwd),
        ]
        if policy.read_only_root:
            argv.append("--read-only")
        if policy.memory:
            argv += ["--memory", policy.memory]
        if policy.cpus is not None:
            argv += ["--cpus", str(policy.cpus)]
        argv += ["--tmpfs", f"/tmp:rw,nosuid,nodev,size={policy.tmpfs_size}"]
        if policy.user == "host" and os.name == "posix":
            argv += ["--user", f"{os.getuid()}:{os.getgid()}"]

        argv += ["--mount", _mount(cwd, readonly=not writable_cwd)]
        git_dir = self.config.repo_path / ".git"
        if git_dir.exists() and not _is_within(git_dir, cwd):
            argv += ["--mount", _mount(git_dir, readonly=True)]
        if include_state and self.config.state_dir.exists():
            if not _is_within(self.config.state_dir, cwd):
                argv += ["--mount", _mount(self.config.state_dir, readonly=True)]

        env_names = _configured_env_names(self.config, scope)
        env_names.update((env or {}).keys())
        env_names.update({"GIT_OPTIONAL_LOCKS", "HOME", "XDG_CACHE_HOME"})
        for name in sorted(env_names):
            if name in process_env:
                argv += ["--env", name]

        argv.append(image)
        argv.extend(_inner_command(command, shell=shell))
        return subprocess.run(
            argv,
            cwd=cwd,
            env=process_env,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            timeout=timeout,
            check=False,
        )
