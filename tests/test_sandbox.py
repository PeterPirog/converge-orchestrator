from __future__ import annotations

import subprocess
import types
from pathlib import Path, PurePosixPath, PureWindowsPath
from unittest.mock import patch

import pytest

from converge_orchestrator.graph import quality
from converge_orchestrator.models import GateResult, ProjectConfig
from converge_orchestrator.sandbox import (
    ExecutionSandbox,
    SandboxPreflightError,
    _resolve_worktree_git_dirs,
    container_path_for,
    is_windows_form_path,
)

_PINNED_TEST_IMAGE = "converge-runtime@sha256:" + "a" * 64


def _container_config(tmp_path: Path) -> ProjectConfig:
    repo = tmp_path / "repo"
    repo.mkdir(exist_ok=True)
    (repo / ".git").mkdir(exist_ok=True)
    requirements = tmp_path / "architecture.md"
    requirements.write_text("System must remain isolated.\n", encoding="utf-8")
    return ProjectConfig(
        repo_path=repo,
        requirements_path=requirements,
        require_spec_read_only=False,
        model_gateway={
            "kind": "openwebui",
            "api_key_env": "OPENWEBUI_API_KEY",
        },
        sandbox={
            "mode": "container",
            "image": _PINNED_TEST_IMAGE,
            "agent_network": "converge-ai",
            "quality_network": "none",
            "agent_gateway_base_url": "http://open-webui:8080/api",
            "pass_env": ["PROJECT_TOKEN"],
        },
        agents={},
    )


def _env_names(argv: list[str]) -> set[str]:
    names: set[str] = set()
    for index, value in enumerate(argv[:-1]):
        if value == "--env":
            names.add(argv[index + 1])
    return names


def _mounts(argv: list[str]) -> dict[str, tuple[str, bool]]:
    """Map bind-mount source -> (container destination, readonly) from docker argv."""
    parsed: dict[str, tuple[str, bool]] = {}
    for index, value in enumerate(argv[:-1]):
        if value != "--mount":
            continue
        option = argv[index + 1]
        fields = dict(
            part.split("=", 1) for part in option.split(",") if "=" in part
        )
        parsed[fields["src"]] = (fields["dst"], option.endswith(",readonly"))
    return parsed


def _internal_network_result():
    return types.SimpleNamespace(returncode=0, stdout="true\n")


def test_container_rejects_mutable_image_reference_before_execution(tmp_path: Path) -> None:
    cfg = _container_config(tmp_path)
    cfg.sandbox.image = "converge-runtime:latest"

    with patch("converge_orchestrator.sandbox.subprocess.run") as runner:
        with pytest.raises(SandboxPreflightError, match="digest-pinned"):
            ExecutionSandbox(cfg).run(
                ["python", "-m", "pytest"],
                cwd=cfg.repo_path,
                scope="quality",
            )

    runner.assert_not_called()


def test_container_agent_uses_hardened_runtime_and_allowlisted_env(
    tmp_path: Path,
    monkeypatch,
) -> None:
    cfg = _container_config(tmp_path)
    monkeypatch.setenv("OPENWEBUI_API_KEY", "test-api-key")
    monkeypatch.setenv("PROJECT_TOKEN", "test-project-token")
    monkeypatch.setenv("HOST_ONLY_VALUE", "must-not-enter-container")
    completed = types.SimpleNamespace(returncode=0, stdout="ok")

    with (
        patch(
            "converge_orchestrator.sandbox.run_configured",
            return_value=_internal_network_result(),
        ),
        patch("converge_orchestrator.sandbox.subprocess.run", return_value=completed) as run,
    ):
        result = ExecutionSandbox(cfg).run(
            ["opencode", "run"],
            cwd=cfg.repo_path,
            scope="agent",
            writable_cwd=False,
        )

    assert result.returncode == 0
    argv = run.call_args.args[0]
    assert "--pull=never" in argv
    assert "--cap-drop=ALL" in argv
    assert "no-new-privileges:true" in argv
    assert "--read-only" in argv
    assert argv[argv.index("--entrypoint") + 1] == ""
    assert argv[argv.index("--network") + 1] == "converge-ai"
    assert _PINNED_TEST_IMAGE in argv
    mounts = [argv[index + 1] for index, value in enumerate(argv[:-1]) if value == "--mount"]
    assert any(str(cfg.repo_path) in mount and mount.endswith(",readonly") for mount in mounts)
    passed = _env_names(argv)
    assert "OPENWEBUI_API_KEY" in passed
    assert "PROJECT_TOKEN" in passed
    assert "HOST_ONLY_VALUE" not in passed


def test_builder_worktree_and_git_pointer_have_distinct_permissions(
    tmp_path: Path,
    monkeypatch,
) -> None:
    cfg = _container_config(tmp_path)
    worktree = tmp_path / "state" / "worktrees" / "arch-001"
    worktree.mkdir(parents=True)
    worktree_git = worktree / ".git"
    worktree_git.write_text(
        f"gitdir: {cfg.repo_path / '.git' / 'worktrees' / 'arch-001'}\n",
        encoding="utf-8",
    )
    completed = types.SimpleNamespace(returncode=0, stdout="ok")
    # The fixture is not a real git repository, so host git resolution cannot succeed;
    # pin that fallback explicitly to prove the historical pointer-mount behavior.
    monkeypatch.setattr(
        "converge_orchestrator.sandbox._resolve_worktree_git_dirs",
        lambda _cwd: None,
    )

    with (
        patch(
            "converge_orchestrator.sandbox.run_configured",
            return_value=_internal_network_result(),
        ),
        patch("converge_orchestrator.sandbox.subprocess.run", return_value=completed) as run,
    ):
        ExecutionSandbox(cfg).run(
            ["opencode", "run"],
            cwd=worktree,
            scope="agent",
            writable_cwd=True,
        )

    argv = run.call_args.args[0]
    mounts = _mounts(argv)
    worktree_src = str(worktree.resolve())
    pointer_src = str(worktree_git.resolve())
    git_src = str((cfg.repo_path / ".git").resolve())
    assert worktree_src in mounts
    assert mounts[worktree_src][1] is False
    assert mounts[worktree_src][0] == container_path_for(worktree_src)
    assert pointer_src in mounts
    assert mounts[pointer_src][1] is True
    assert mounts[pointer_src][0] == container_path_for(pointer_src)
    assert git_src in mounts
    assert mounts[git_src][1] is True


def test_quality_network_is_separate_from_agent_network(tmp_path: Path) -> None:
    cfg = _container_config(tmp_path)
    completed = types.SimpleNamespace(returncode=0, stdout="ok")

    with patch("converge_orchestrator.sandbox.subprocess.run", return_value=completed) as run:
        ExecutionSandbox(cfg).run(
            ["python", "-m", "pytest"],
            cwd=cfg.repo_path,
            scope="quality",
            writable_cwd=True,
        )

    argv = run.call_args.args[0]
    assert argv[argv.index("--network") + 1] == "none"


def test_agent_container_requires_a_real_docker_internal_network(tmp_path: Path) -> None:
    cfg = _container_config(tmp_path)
    external_network = types.SimpleNamespace(returncode=0, stdout="false\n")

    with patch("converge_orchestrator.sandbox.run_configured", return_value=external_network):
        with pytest.raises(SandboxPreflightError, match="not Docker-internal"):
            ExecutionSandbox(cfg).run(
                ["opencode", "run"],
                cwd=cfg.repo_path,
                scope="agent",
                writable_cwd=False,
            )

    for network in ("none", "host"):
        cfg.sandbox.agent_network = network
        with pytest.raises(SandboxPreflightError, match="named internal agent network"):
            ExecutionSandbox(cfg).run(
                ["opencode", "run"],
                cwd=cfg.repo_path,
                scope="agent",
                writable_cwd=False,
            )


def test_agent_container_rejects_loopback_gateway_and_attach_server(tmp_path: Path) -> None:
    cfg = _container_config(tmp_path)
    cfg.sandbox.agent_gateway_base_url = "http://127.0.0.1:3000/api"
    with pytest.raises(SandboxPreflightError, match="loopback model gateway"):
        ExecutionSandbox(cfg).run(
            ["opencode", "run"],
            cwd=cfg.repo_path,
            scope="agent",
            writable_cwd=False,
        )

    cfg = _container_config(tmp_path)
    cfg.opencode_attach_url = "http://opencode:4096"
    with pytest.raises(SandboxPreflightError, match="attach_url is incompatible"):
        ExecutionSandbox(cfg).run(
            ["opencode", "run"],
            cwd=cfg.repo_path,
            scope="agent",
            writable_cwd=False,
        )


def test_timed_out_container_is_force_removed(tmp_path: Path) -> None:
    cfg = _container_config(tmp_path)
    timeout = subprocess.TimeoutExpired(cmd=["docker", "run"], timeout=7)
    cleaned = types.SimpleNamespace(returncode=0, stdout="removed")

    with patch(
        "converge_orchestrator.sandbox.subprocess.run",
        side_effect=[timeout, cleaned],
    ) as run:
        with pytest.raises(subprocess.TimeoutExpired):
            ExecutionSandbox(cfg).run(
                ["python", "-m", "pytest"],
                cwd=cfg.repo_path,
                timeout=7,
                scope="quality",
                writable_cwd=True,
            )

    assert run.call_count == 2
    cleanup = run.call_args_list[1].args[0]
    assert cleanup[:3] == ["docker", "rm", "-f"]
    assert cleanup[3].startswith("converge-")


def test_active_graph_measures_scope_only_after_quality_commands(tmp_path: Path) -> None:
    cfg = ProjectConfig(
        repo_path=tmp_path,
        requirements_path=tmp_path / "architecture.md",
        require_spec_read_only=False,
        agents={},
    )
    state = {
        "config_path": str(tmp_path / "converge.yaml"),
        "run_id": "run-1",
        "task": {
            "id": "ARCH-001-1",
            "requirement_ids": ["ARCH-001"],
            "title": "bounded",
            "objective": "bounded",
            "allowed_paths": ["src/**"],
        },
        "worktree": str(tmp_path),
    }
    order: list[str] = []
    gate = GateResult(name="tests", ok=True, required=True, returncode=0, output="")
    scope = GateResult(name="diff_scope", ok=True, required=True, returncode=0, output="")
    store = types.SimpleNamespace(write_json=lambda *args, **kwargs: None)

    with (
        patch("converge_orchestrator.graph.load_config", return_value=cfg),
        patch(
            "converge_orchestrator.graph.run_quality_gates",
            side_effect=lambda *args: order.append("quality") or [gate],
        ),
        patch(
            "converge_orchestrator.graph.run_scope_gate",
            side_effect=lambda *args: order.append("scope") or scope,
        ),
        patch("converge_orchestrator.graph.wf._evidence", return_value=store),
    ):
        result = quality(state)

    assert order == ["quality", "scope"]
    assert [item["name"] for item in result["quality_results"]] == [
        "tdd_green",
        "tests",
        "diff_scope",
    ]
    assert result["quality_results"][0]["required"] is False


def test_windows_host_paths_map_to_deterministic_posix_container_paths() -> None:
    assert is_windows_form_path(r"C:\Users\Ila\project") is True
    assert is_windows_form_path("C:/Users/Ila/project") is True
    assert is_windows_form_path("/srv/converge/repo") is False
    assert (
        container_path_for(PureWindowsPath(r"C:\Users\Ila\project\repo"))
        == "/converge/host/c/Users/Ila/project/repo"
    )
    assert (
        container_path_for("C:/Users/Ila/project/repo")
        == "/converge/host/c/Users/Ila/project/repo"
    )
    assert container_path_for("D:\\Data\\Repo") == "/converge/host/d/Data/Repo"


def test_posix_paths_keep_identity_container_mapping() -> None:
    assert container_path_for(PurePosixPath("/srv/converge/repo")) == "/srv/converge/repo"
    assert container_path_for("/srv/converge/repo") == "/srv/converge/repo"


def test_unsupported_windows_path_forms_fail_closed() -> None:
    with pytest.raises(SandboxPreflightError, match="UNC"):
        container_path_for(r"\\server\share\repo")
    with pytest.raises(SandboxPreflightError, match="UNC"):
        container_path_for(r"\\?\C:\repo")


def test_container_workdir_is_posix_and_equals_cwd_mount_destination(tmp_path: Path) -> None:
    cfg = _container_config(tmp_path)
    completed = types.SimpleNamespace(returncode=0, stdout="ok")

    with patch("converge_orchestrator.sandbox.subprocess.run", return_value=completed) as run:
        ExecutionSandbox(cfg).run(
            ["python", "-m", "pytest"],
            cwd=cfg.repo_path,
            scope="quality",
            writable_cwd=True,
        )

    argv = run.call_args.args[0]
    workdir = argv[argv.index("--workdir") + 1]
    mounts = _mounts(argv)
    repo_src = str(cfg.repo_path.resolve())
    assert mounts[repo_src][0] == workdir
    assert PurePosixPath(workdir).is_absolute()
    assert "\\" not in workdir
    assert not is_windows_form_path(workdir)
    assert workdir == container_path_for(repo_src)
    assert mounts[repo_src][1] is False


def test_mount_source_stays_host_path_and_destination_is_posix(tmp_path: Path) -> None:
    cfg = _container_config(tmp_path)
    shared = tmp_path / "shared"
    shared.mkdir()
    completed = types.SimpleNamespace(returncode=0, stdout="ok")

    with patch("converge_orchestrator.sandbox.subprocess.run", return_value=completed) as run:
        ExecutionSandbox(cfg).run(
            ["python", "-m", "pytest"],
            cwd=cfg.repo_path,
            scope="quality",
            writable_cwd=False,
            readonly_paths=(shared,),
        )

    mounts = _mounts(run.call_args.args[0])
    repo_src = str(cfg.repo_path.resolve())
    shared_src = str(shared.resolve())
    assert mounts[repo_src] == (container_path_for(repo_src), True)
    assert mounts[shared_src] == (container_path_for(shared_src), True)


def test_declared_path_arguments_and_config_env_are_container_visible(tmp_path: Path) -> None:
    cfg = _container_config(tmp_path)
    cfg.state_dir = tmp_path / "state"
    generated_config = cfg.state_dir / "opencode.generated.json"
    generated_config.parent.mkdir(parents=True, exist_ok=True)
    generated_config.write_text("{}", encoding="utf-8")
    managed_dir = cfg.state_dir / "opencode-runtime" / "planner"
    managed_dir.mkdir(parents=True, exist_ok=True)
    prompt = "IMPLEMENT REQUIREMENT PROMPT"
    command = ["opencode", "run", "--format", "json", "--dir", str(cfg.repo_path), prompt]
    completed = types.SimpleNamespace(returncode=0, stdout="ok")

    with (
        patch(
            "converge_orchestrator.sandbox.run_configured",
            return_value=_internal_network_result(),
        ),
        patch("converge_orchestrator.sandbox.subprocess.run", return_value=completed) as run,
    ):
        ExecutionSandbox(cfg).run(
            command,
            cwd=cfg.repo_path,
            env={"OPENCODE_CONFIG_CONTENT": "{\"agent\":{}}"},
            path_env={
                "OPENCODE_CONFIG": generated_config,
                "OPENCODE_CONFIG_DIR": managed_dir,
            },
            path_arguments=(5,),
            scope="agent",
            writable_cwd=False,
            readonly_paths=(generated_config, managed_dir),
        )

    argv = run.call_args.args[0]
    inner = argv[argv.index(_PINNED_TEST_IMAGE) + 1 :]
    dir_index = inner.index("--dir")
    assert inner[dir_index + 1] == container_path_for(str(cfg.repo_path.resolve()))
    assert inner[dir_index + 2] == prompt
    mounts = _mounts(argv)
    process_env = run.call_args.kwargs["env"]
    expected_config = container_path_for(str(generated_config.resolve()))
    expected_dir = container_path_for(str(managed_dir.resolve()))
    assert process_env["OPENCODE_CONFIG"] == expected_config
    assert process_env["OPENCODE_CONFIG"] == mounts[str(generated_config.resolve())][0]
    assert process_env["OPENCODE_CONFIG_DIR"] == expected_dir
    assert process_env["OPENCODE_CONFIG_DIR"] == mounts[str(managed_dir.resolve())][0]
    assert process_env["OPENCODE_CONFIG_CONTENT"] == "{\"agent\":{}}"
    passed = _env_names(argv)
    assert {"OPENCODE_CONFIG", "OPENCODE_CONFIG_DIR", "OPENCODE_CONFIG_CONTENT"} <= passed


def test_declared_path_arguments_fail_closed(tmp_path: Path) -> None:
    cfg = _container_config(tmp_path)

    with pytest.raises(SandboxPreflightError, match="requires a list command"):
        ExecutionSandbox(cfg).run(
            "python -m pytest",
            cwd=cfg.repo_path,
            scope="quality",
            path_arguments=(0,),
        )

    with pytest.raises(SandboxPreflightError, match="outside the command"):
        ExecutionSandbox(cfg).run(
            ["python"],
            cwd=cfg.repo_path,
            scope="quality",
            path_arguments=(3,),
        )


def test_windows_worktree_pointer_mounts_git_metadata_readonly(
    tmp_path: Path,
    monkeypatch,
) -> None:
    cfg = _container_config(tmp_path)
    worktree = tmp_path / "state" / "worktrees" / "arch-001"
    worktree.mkdir(parents=True)
    (worktree / ".git").write_text(
        "gitdir: C:\\repo\\.git\\worktrees\\arch-001\n",
        encoding="utf-8",
    )
    common_dir = tmp_path / "common-git"
    common_dir.mkdir()
    fake_gitdir = PureWindowsPath(r"C:\repo\.git\worktrees\arch-001")
    monkeypatch.setattr(
        "converge_orchestrator.sandbox._resolve_worktree_git_dirs",
        lambda _cwd: (Path(str(fake_gitdir)), common_dir),
    )
    completed = types.SimpleNamespace(returncode=0, stdout="ok")

    with (
        patch(
            "converge_orchestrator.sandbox.run_configured",
            return_value=_internal_network_result(),
        ),
        patch("converge_orchestrator.sandbox.subprocess.run", return_value=completed) as run,
    ):
        ExecutionSandbox(cfg).run(
            ["opencode", "run"],
            cwd=worktree,
            scope="agent",
            writable_cwd=True,
        )

    argv = run.call_args.args[0]
    mounts = _mounts(argv)
    worktree_src = str(worktree.resolve())
    common_src = str(common_dir.resolve())
    assert mounts[worktree_src][1] is False
    assert mounts[common_src] == (container_path_for(common_src), True)
    assert str((worktree / ".git").resolve()) not in mounts
    process_env = run.call_args.kwargs["env"]
    assert process_env["GIT_DIR"] == container_path_for(str(fake_gitdir))
    assert process_env["GIT_WORK_TREE"] == container_path_for(worktree_src)
    assert process_env["GIT_WORK_TREE"] == mounts[worktree_src][0]
    assert {"GIT_DIR", "GIT_WORK_TREE"} <= _env_names(argv)


def test_posix_worktree_pointer_keeps_identity_git_pointer_mount(
    tmp_path: Path,
    monkeypatch,
) -> None:
    cfg = _container_config(tmp_path)
    worktree = tmp_path / "state" / "worktrees" / "arch-001"
    worktree.mkdir(parents=True)
    worktree_git = worktree / ".git"
    worktree_git.write_text(
        f"gitdir: {cfg.repo_path / '.git' / 'worktrees' / 'arch-001'}\n",
        encoding="utf-8",
    )
    fake_gitdir = PurePosixPath("/srv/converge/repo/.git/worktrees/arch-001")
    fake_common = PurePosixPath("/srv/converge/repo/.git")
    monkeypatch.setattr(
        "converge_orchestrator.sandbox._resolve_worktree_git_dirs",
        lambda _cwd: (Path(str(fake_gitdir)), Path(str(fake_common))),
    )
    completed = types.SimpleNamespace(returncode=0, stdout="ok")

    with (
        patch(
            "converge_orchestrator.sandbox.run_configured",
            return_value=_internal_network_result(),
        ),
        patch("converge_orchestrator.sandbox.subprocess.run", return_value=completed) as run,
    ):
        ExecutionSandbox(cfg).run(
            ["opencode", "run"],
            cwd=worktree,
            scope="agent",
            writable_cwd=True,
        )

    argv = run.call_args.args[0]
    mounts = _mounts(argv)
    pointer_src = str(worktree_git.resolve())
    assert mounts[pointer_src][1] is True
    process_env = run.call_args.kwargs["env"]
    assert "GIT_DIR" not in process_env
    assert "GIT_WORK_TREE" not in process_env
    assert {"GIT_DIR", "GIT_WORK_TREE"}.isdisjoint(_env_names(argv))


def test_real_linked_worktree_mounts_readonly_git_metadata(tmp_path: Path) -> None:
    cfg = _container_config(tmp_path)
    repo = cfg.repo_path
    placeholder_git = repo / ".git"
    if placeholder_git.is_dir():
        placeholder_git.rmdir()
    (repo / "README.md").write_text("readme\n", encoding="utf-8")
    worktree = tmp_path / "worktree"
    for args in (
        ["git", "init", "-q", str(repo)],
        ["git", "-C", str(repo), "config", "user.email", "converge@example.test"],
        ["git", "-C", str(repo), "config", "user.name", "Converge Test"],
        ["git", "-C", str(repo), "add", "-A"],
        ["git", "-C", str(repo), "commit", "-q", "-m", "init"],
        [
            "git",
            "-C",
            str(repo),
            "worktree",
            "add",
            "-q",
            str(worktree),
            "-b",
            "task-1",
        ],
    ):
        subprocess.run(args, check=True, capture_output=True)
    completed = types.SimpleNamespace(returncode=0, stdout="ok")
    # Resolve the real host git dirs BEFORE patching subprocess.run, because the docker
    # argv mock would otherwise also intercept the host git rev-parse subprocesses.
    real_dirs = _resolve_worktree_git_dirs(worktree)
    assert real_dirs is not None

    with (
        patch(
            "converge_orchestrator.sandbox._resolve_worktree_git_dirs",
            return_value=real_dirs,
        ),
        patch("converge_orchestrator.sandbox.subprocess.run", return_value=completed) as run,
    ):
        ExecutionSandbox(cfg).run(
            ["python", "-m", "pytest"],
            cwd=worktree,
            scope="quality",
            writable_cwd=True,
        )

    argv = run.call_args.args[0]
    mounts = _mounts(argv)
    git_src = str((repo / ".git").resolve())
    assert git_src in mounts
    assert mounts[git_src][1] is True
    gitdir = real_dirs[0]
    process_env = run.call_args.kwargs["env"]
    pointer_src = str((worktree / ".git").resolve())
    if is_windows_form_path(str(gitdir)):
        # A Windows pointer is unusable inside the container, so it stays unmounted.
        assert pointer_src not in mounts
        assert process_env["GIT_DIR"] == container_path_for(str(gitdir))
        assert process_env["GIT_WORK_TREE"] == container_path_for(str(worktree.resolve()))
        assert {"GIT_DIR", "GIT_WORK_TREE"} <= _env_names(argv)
    else:
        # A POSIX pointer is container-consumable: historical behavior is preserved.
        assert mounts[pointer_src] == (container_path_for(pointer_src), True)
        assert "GIT_DIR" not in process_env
        assert "GIT_WORK_TREE" not in process_env
