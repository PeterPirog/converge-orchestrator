"""Lock INV-STATE-002: a single authoritative control-state topology identity per run.

INV-STATE-002 (CONVERGE_ORCHESTRATOR_2_0_SOURCE_OF_TRUTH.md:L153 [4. Global invariants]):
One active or recoverable run SHALL have exactly one authoritative control-state topology
identity, and conflicting registry, checkpoint, evidence, budget, or ownership stores SHALL
NOT each be treated as valid workflow authority for that run.

These deterministic offline tests pin the unchanged runtime's single-identity contract. The
registry binding ``(workspace_id, state_store_id)`` is the one authoritative control-state
topology identity for a run: a run whose pinned configuration points at a different Git
workspace or a different durable state store is rejected by ``RunController._config_for_run``
with a ``WorkspaceAffinityError`` and is skipped by the recovery scanner
``ScheduledRunController._unfinished_records`` so it is never recovered or treated as valid
workflow authority; a run whose pinned configuration matches the single bound identity is
accepted. No network, model, provider, or git-remote access is used; the graph is never
built and no worker process is spawned.
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Any

import pytest
import yaml

from converge_orchestrator.config import materialize_run_config_snapshot
from converge_orchestrator.registry import ControlRegistry
from converge_orchestrator.runtime_service import ScheduledRunController
from converge_orchestrator.workspace_identity import (
    WorkspaceAffinityError,
    state_store_id,
    workspace_id,
)

_PROJECT_ID = "topology-proj"


def _git_repo(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        ["git", "init", "-b", "main", str(path)],
        check=True,
        capture_output=True,
        text=True,
    )
    return path


def _write_config(tmp_path: Path, *, repo: Path, state_dir: Path) -> Path:
    tmp_path.mkdir(parents=True, exist_ok=True)
    requirements = tmp_path / "requirements.md"
    requirements.write_text(
        "A run has exactly one authoritative control-state topology identity.\n",
        encoding="utf-8",
    )
    config = tmp_path / "converge.yaml"
    config.write_text(
        yaml.safe_dump(
            {
                "version": 1,
                "project": {
                    "name": "topology",
                    "repo_path": str(repo),
                    "requirements_path": str(requirements),
                    "state_dir": str(state_dir),
                    "worktree_dir": str(state_dir / "worktrees"),
                    "require_spec_read_only": False,
                },
                "agents": {},
            },
            sort_keys=True,
        ),
        encoding="utf-8",
    )
    return config


class _TopologyWorld:
    """A durable world with one authoritative topology identity bound in the registry."""

    def __init__(self, tmp_path: Path) -> None:
        self.repo = _git_repo(tmp_path / "repo-bound")
        self.other_repo = _git_repo(tmp_path / "repo-foreign")
        self.state = tmp_path / "state-bound"
        self.other_state = tmp_path / "state-foreign"

        # The registry binding is the single authoritative control-state topology identity.
        self.bound_workspace = workspace_id(self.repo)
        self.bound_state_store = state_store_id(self.state)

        self.registry = ControlRegistry(self.state / "control.sqlite")
        self.registry.register_project(
            _PROJECT_ID,
            tmp_path / "bound.yaml",
            workspace_id=self.bound_workspace,
            state_store_id=self.bound_state_store,
        )

        # Pinned configuration matching the bound identity (same workspace and state store).
        self.config_matching = _write_config(
            tmp_path / "cfg-match", repo=self.repo, state_dir=self.state
        )
        # Pinned configuration from a different Git workspace (workspace conflict).
        self.config_ws_conflict = _write_config(
            tmp_path / "cfg-ws", repo=self.other_repo, state_dir=self.state
        )
        # Pinned configuration from a different durable state store (state-store conflict).
        self.config_ss_conflict = _write_config(
            tmp_path / "cfg-ss", repo=self.repo, state_dir=self.other_state
        )

    def _controller(self) -> ScheduledRunController:
        controller = object.__new__(ScheduledRunController)
        controller.registry = self.registry
        return controller

    def _pin(self, config: Path, run_id: str, thread_id: str) -> dict[str, Any]:
        _, snapshot_path, digest = materialize_run_config_snapshot(config, run_id)
        return self.registry.create_run(
            run_id,
            _PROJECT_ID,
            thread_id,
            config_snapshot_path=snapshot_path,
            config_snapshot_sha256=digest,
        )


@pytest.fixture
def world(tmp_path: Path) -> _TopologyWorld:
    return _TopologyWorld(tmp_path)


def test_matching_pinned_config_resolves_to_the_bound_identity(world: _TopologyWorld) -> None:
    """Positive control: a pinned config matching the bound identity is accepted."""
    record = world._pin(world.config_matching, "run-match", "thread-match")
    controller = world._controller()

    cfg = controller._config_for_run(record)

    assert cfg.repo_path == world.repo.resolve()
    assert cfg.state_dir == world.state.resolve()


def test_pinned_config_from_a_conflicting_workspace_is_rejected(world: _TopologyWorld) -> None:
    """A run pinned to a different Git workspace is not valid workflow authority."""
    record = world._pin(world.config_ws_conflict, "run-ws", "thread-ws")
    controller = world._controller()

    with pytest.raises(WorkspaceAffinityError, match="bound to workspace"):
        controller._config_for_run(record)


def test_pinned_config_from_a_conflicting_state_store_is_rejected(world: _TopologyWorld) -> None:
    """A run pinned to a different durable state store is not valid workflow authority."""
    record = world._pin(world.config_ss_conflict, "run-ss", "thread-ss")
    controller = world._controller()

    with pytest.raises(WorkspaceAffinityError, match="bound to state store"):
        controller._config_for_run(record)


def test_recovery_scanner_skips_conflicting_runs(world: _TopologyWorld) -> None:
    """The recovery scanner yields only the run matching the single bound identity."""
    ok = world._pin(world.config_matching, "run-ok", "thread-ok")
    ws = world._pin(world.config_ws_conflict, "run-ws", "thread-ws")
    ss = world._pin(world.config_ss_conflict, "run-ss", "thread-ss")
    controller = world._controller()

    yielded = list(controller._unfinished_records())
    yielded_ids = {record["id"] for record in yielded}

    assert ok["id"] in yielded_ids
    assert ws["id"] not in yielded_ids
    assert ss["id"] not in yielded_ids

    # A conflicting run is skipped, never surfaced as a recovered error authority.
    assert world.registry.get_run(ws["id"])["error"] is None
    assert world.registry.get_run(ss["id"])["error"] is None
