from __future__ import annotations

import ipaddress
import os
import re
import shlex
import shutil
import subprocess
import uuid
from collections.abc import Mapping, Sequence
from pathlib import Path, PurePath
from typing import Any, Literal
from urllib.parse import urlparse

from .models import ProjectConfig
from .opencode_config import role_mcp_env_source
from .shell import run_configured

SandboxScope = Literal["agent", "quality"]
_ENV_REFERENCE = re.compile(r"\{env:([A-Za-z_][A-Za-z0-9_]*)\}")
_IMAGE_DIGEST_REFERENCE = re.compile(
    r"^(?:sha256:[0-9a-f]{64}|[^@\s]+@sha256:[0-9a-f]{64})$"
)
_HOST_AGENT_BASE_ENV = {
    "APPDATA",
    "COMSPEC",
    "HOME",
    "HOMEDRIVE",
    "HOMEPATH",
    "LANG",
    "LC_ALL",
    "LOCALAPPDATA",
    "PATH",
    "PATHEXT",
    "PROGRAMDATA",
    "SHELL",
    "SYSTEMDRIVE",
    "SYSTEMROOT",
    "TEMP",
    "TMP",
    "TMPDIR",
    "USERPROFILE",
    "WINDIR",
}


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


def _configured_env_names(
    config: ProjectConfig,
    scope: SandboxScope,
    agent_role: str | None = None,
) -> set[str]:
    names = set(config.sandbox.pass_env)
    if scope != "agent":
        return names
    if config.model_gateway.api_key_env:
        names.add(config.model_gateway.api_key_env)
    names.update(_env_references(config.model_gateway.headers))
    names.update(_env_references(role_mcp_env_source(config, agent_role)))
    return names


def _host_agent_environment(
    config: ProjectConfig,
    env: dict[str, str] | None,
    agent_role: str | None,
) -> dict[str, str]:
    """Build a least-privilege host environment instead of inheriting every parent secret."""
    names = _HOST_AGENT_BASE_ENV | _configured_env_names(config, "agent", agent_role)
    process_env = {name: os.environ[name] for name in names if name in os.environ}
    if env:
        process_env.update(env)
    process_env["GIT_OPTIONAL_LOCKS"] = "0"
    return process_env


_CONTAINER_HOST_MOUNT_ROOT = "/converge/host"
_WINDOWS_DRIVE_PATH = re.compile(r"^[A-Za-z]:[\\/]")


def is_windows_form_path(text: str) -> bool:
    """Return whether text is a Windows drive path form (C:\\ or C:/ prefix)."""
    return _WINDOWS_DRIVE_PATH.match(text) is not None


def container_path_for(path: str | PurePath) -> str:
    """Map a host path to its deterministic container-side absolute path.

    POSIX-form paths keep the historical identity mapping. Windows drive paths
    map into a fixed POSIX namespace (/converge/host/<drive>/...) so Linux
    containers always receive a POSIX absolute workdir and bind destination.
    UNC and device path forms are rejected instead of guessed.
    """
    text = str(path)
    if _WINDOWS_DRIVE_PATH.match(text):
        drive = text[0].lower()
        remainder = text[2:].replace("\\", "/").strip("/")
        return f"{_CONTAINER_HOST_MOUNT_ROOT}/{drive}/{remainder}"
    if text.startswith("\\\\") or text.startswith("//"):
        raise SandboxPreflightError(
            f"container sandbox cannot map UNC/device host paths safely: {text!r}; "
            "use a local drive path"
        )
    return text


def _mount(source: Path, *, readonly: bool, destination: str | None = None) -> str:
    """Build a bind-mount option whose source stays the real host path.

    Docker resolves the source on the host side, so it must remain a Windows
    host path on Windows. The container-side destination is the deterministic
    mapped path so a Linux container never receives a Windows path as a bind
    destination or workdir.
    """
    resolved = source.resolve()
    src_text = str(resolved)
    if any(character in src_text for character in (",", '"')):
        raise SandboxPreflightError(
            f"bind mount source cannot be represented safely in --mount: {src_text!r}"
        )
    dst_text = destination if destination is not None else container_path_for(resolved)
    option = f"type=bind,src={src_text},dst={dst_text}"
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


def _translate_declared_path_arguments(
    command: str | list[str],
    path_arguments: Sequence[int],
) -> str | list[str]:
    """Translate declared command arguments that refer to mounted host paths.

    Windows-form absolute arguments resolve on the host and map to the same
    deterministic container path used as their bind destination. POSIX or
    relative arguments pass through unchanged. Declared indexes must exist;
    declaring path arguments on a non-list command fails closed instead of
    silently sending a Windows host path into a Linux container.
    """
    if not path_arguments:
        return command
    if not isinstance(command, list):
        raise SandboxPreflightError(
            "path_arguments requires a list command with declared argument indexes"
        )
    translated = [str(item) for item in command]
    for index in path_arguments:
        if not isinstance(index, int) or not 0 <= index < len(translated):
            raise SandboxPreflightError(
                f"declared path argument index is outside the command: {index!r}"
            )
        value = translated[index]
        if is_windows_form_path(value):
            translated[index] = container_path_for(Path(value).resolve())
    return translated


def _host_git_rev_parse_abs(cwd: Path, option: str) -> Path | None:
    try:
        result = run_configured(
            ["git", "-C", str(cwd), "rev-parse", option],
            cwd=cwd,
            timeout=30,
        )
    except OSError:
        return None
    if result.returncode != 0:
        return None
    text = result.stdout.strip()
    if not text:
        return None
    candidate = Path(text)
    if not candidate.is_absolute():
        candidate = cwd / candidate
    return Path(os.path.abspath(candidate))


def _resolve_worktree_git_dirs(cwd: Path) -> tuple[Path, Path] | None:
    """Resolve (worktree git dir, common git dir) on the host for a linked worktree.

    Windows writes worktree `.git` pointers as Windows host paths that a Linux
    container cannot consume. Resolution therefore happens on the host so the
    container can receive a read-only common-git mount plus container-visible
    GIT_DIR/GIT_WORK_TREE. Returns None when git is unavailable or fails; the
    caller then preserves the historical behavior.
    """
    git_dir = _host_git_rev_parse_abs(cwd, "--git-dir")
    common_dir = _host_git_rev_parse_abs(cwd, "--git-common-dir")
    if git_dir is None or common_dir is None:
        return None
    return git_dir, common_dir


def _is_loopback_url(url: str | None) -> bool:
    if not url:
        return False
    hostname = urlparse(url).hostname
    if not hostname:
        return False
    if hostname.lower() == "localhost":
        return True
    try:
        return ipaddress.ip_address(hostname).is_loopback
    except ValueError:
        return False


def _is_digest_pinned_image(image: str) -> bool:
    """Return whether Docker/OCI execution is bound to immutable sha256 content."""
    return _IMAGE_DIGEST_REFERENCE.fullmatch(image.strip()) is not None


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

    def _validate_image_policy(self) -> str:
        image = self.config.sandbox.image
        if not image:
            raise SandboxPreflightError("sandbox image is not configured")
        if not _is_digest_pinned_image(image):
            raise SandboxPreflightError(
                "container sandbox image must be immutable and digest-pinned as "
                "repository@sha256:<64-hex> (or local sha256:<64-hex> image ID); "
                f"mutable image references are forbidden: {image}"
            )
        return image

    def _validate_agent_runtime(self) -> None:
        if self.config.opencode_attach_url:
            raise SandboxPreflightError(
                "opencode.attach_url is incompatible with container sandboxing because the "
                "attached server would execute tools outside Converge's process boundary"
            )
        gateway = self.config.model_gateway
        if gateway.kind != "existing":
            runtime_url = self.config.sandbox.agent_gateway_base_url or gateway.base_url
            if _is_loopback_url(runtime_url):
                raise SandboxPreflightError(
                    "sandboxed agents cannot use a loopback model gateway; configure "
                    "sandbox.agent_gateway_base_url to an endpoint on the agent network"
                )

    def _inspect_network(self, network: str) -> bool:
        policy = self.config.sandbox
        result = run_configured(
            [
                policy.engine,
                "network",
                "inspect",
                "--format",
                "{{.Internal}}",
                network,
            ],
            cwd=self.config.repo_path,
            timeout=30,
        )
        if result.returncode != 0:
            raise SandboxPreflightError(
                f"configured sandbox network does not exist: {network}"
            )
        return result.stdout.strip().lower() == "true"

    def _validate_internal_agent_network(self) -> None:
        policy = self.config.sandbox
        if not policy.require_internal_agent_network:
            return
        self._validate_network_policy()
        if not self._inspect_network(policy.agent_network):
            raise SandboxPreflightError(
                f"sandbox agent network is not Docker-internal: {policy.agent_network}"
            )

    def preflight(self) -> None:
        policy = self.config.sandbox
        if policy.mode == "host":
            return
        self._validate_network_policy()
        self._validate_agent_runtime()
        image = self._validate_image_policy()
        if shutil.which(policy.engine) is None:
            raise SandboxPreflightError(
                f"sandbox engine executable not found on PATH: {policy.engine}"
            )
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
            internal = self._inspect_network(network)
            if (
                network == policy.agent_network
                and policy.require_internal_agent_network
                and not internal
            ):
                raise SandboxPreflightError(
                    f"sandbox agent network is not Docker-internal: {network}"
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
        agent_role: str | None = None,
        readonly_paths: tuple[Path, ...] = (),
        path_arguments: Sequence[int] = (),
        path_env: Mapping[str, Path] | None = None,
    ) -> subprocess.CompletedProcess[str]:
        if self.config.sandbox.mode == "host":
            if path_env:
                # Host mode keeps host-visible path values; container mode translates them.
                env = dict(env or {})
                env.update({name: str(path) for name, path in path_env.items()})
            if scope == "agent":
                return run_configured(
                    command,
                    cwd=cwd,
                    timeout=timeout,
                    shell=shell,
                    env=_host_agent_environment(self.config, env, agent_role),
                    inherit_env=False,
                )
            return run_configured(
                command,
                cwd=cwd,
                timeout=timeout,
                shell=shell,
                env=env,
            )
        self._validate_network_policy()
        self._validate_image_policy()
        if scope == "agent":
            self._validate_agent_runtime()
            self._validate_internal_agent_network()
        return self._run_container(
            command,
            cwd=cwd,
            timeout=timeout,
            shell=shell,
            env=env,
            scope=scope,
            writable_cwd=writable_cwd,
            include_state=include_state,
            agent_role=agent_role,
            readonly_paths=readonly_paths,
            path_arguments=path_arguments,
            path_env=path_env,
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
        agent_role: str | None,
        readonly_paths: tuple[Path, ...],
        path_arguments: Sequence[int],
        path_env: Mapping[str, Path] | None,
    ) -> subprocess.CompletedProcess[str]:
        policy = self.config.sandbox
        image = policy.image
        if not image:
            raise SandboxPreflightError("sandbox image is not configured")

        command = _translate_declared_path_arguments(command, path_arguments)
        cwd = cwd.resolve()
        container_cwd = container_path_for(cwd)
        process_env = os.environ.copy()
        if env:
            process_env.update(env)
        if path_env:
            for name, path_value in path_env.items():
                process_env[name] = container_path_for(path_value.resolve())
        process_env["GIT_OPTIONAL_LOCKS"] = "0"
        process_env["HOME"] = "/tmp/converge-home"
        process_env["XDG_CACHE_HOME"] = "/tmp/converge-cache"

        network = policy.agent_network if scope == "agent" else policy.quality_network
        container_name = f"converge-{uuid.uuid4().hex}"
        argv = [
            policy.engine,
            "run",
            "--rm",
            "--init",
            "--pull=never",
            "--name",
            container_name,
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
            container_cwd,
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

        mount_specs = [_mount(cwd, readonly=not writable_cwd, destination=container_cwd)]
        worktree_env: dict[str, str] = {}
        worktree_git = cwd / ".git"
        if worktree_git.is_file():
            # A linked worktree pointer. Windows writes `gitdir:` as a Windows host path that
            # a Linux container cannot consume, so resolve the real git dirs on the host and
            # expose the common Git metadata read-only at its container path with
            # container-visible GIT_DIR/GIT_WORK_TREE. The pointer itself stays unmounted.
            resolved_dirs = _resolve_worktree_git_dirs(cwd)
            if resolved_dirs is not None and is_windows_form_path(str(resolved_dirs[0])):
                worktree_git_dir, common_git_dir = resolved_dirs
                if not common_git_dir.is_dir():
                    raise SandboxPreflightError(
                        "linked worktree common git directory is missing: "
                        f"{common_git_dir}; refusing to run with unverifiable git metadata"
                    )
                mount_specs.append(_mount(common_git_dir, readonly=True))
                worktree_env = {
                    "GIT_DIR": container_path_for(worktree_git_dir),
                    "GIT_WORK_TREE": container_cwd,
                }
            elif writable_cwd:
                mount_specs.append(_mount(worktree_git, readonly=True))
        git_dir = self.config.repo_path / ".git"
        if git_dir.exists() and not _is_within(git_dir, cwd):
            mount_specs.append(_mount(git_dir, readonly=True))
        if include_state and self.config.state_dir.exists():
            if not _is_within(self.config.state_dir, cwd):
                mount_specs.append(_mount(self.config.state_dir, readonly=True))
        for path in readonly_paths:
            resolved = path.resolve()
            if resolved.exists() and not _is_within(resolved, cwd):
                mount_specs.append(_mount(resolved, readonly=True))
        for option in dict.fromkeys(mount_specs):
            argv += ["--mount", option]
        process_env.update(worktree_env)

        env_names = _configured_env_names(self.config, scope, agent_role)
        env_names.update((env or {}).keys())
        if path_env:
            env_names.update(path_env.keys())
        env_names.update(worktree_env.keys())
        env_names.update({"GIT_OPTIONAL_LOCKS", "HOME", "XDG_CACHE_HOME"})
        for name in sorted(env_names):
            if name in process_env:
                argv += ["--env", name]

        argv.append(image)
        argv.extend(_inner_command(command, shell=shell))
        try:
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
        except subprocess.TimeoutExpired:
            subprocess.run(
                [policy.engine, "rm", "-f", container_name],
                cwd=cwd,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                timeout=30,
                check=False,
            )
            raise
