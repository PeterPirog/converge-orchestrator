"""REQ-CONFIG-010: project configuration permits a statically pinned execution profile
without changing LangGraph workflow topology.

REQ-CONFIG-010 (CONVERGE_ORCHESTRATOR_2_0_SOURCE_OF_TRUTH.md:L208
[6. Project configuration and secret sources]): project configuration SHALL permit either
a statically pinned execution profile or a pinned bootstrap-profile plus trusted
execution-environment resolver policy without changing LangGraph workflow topology.

This deterministic offline test locks the statically pinned execution-profile branch that
the repository implements: a cloud-mode or local-mode configuration (``models.mode`` plus
``models.profile_sets``) -- and the equivalent explicit ``models.profiles`` form -- loads,
pins only the selected profiles into the run snapshot (with ``mode``/``profile_sets`` gone),
and leaves both compiled LangGraph graphs with identical node and edge sets.

Scope note: this test locks ONLY the statically pinned execution-profile branch. It does not
claim coverage of the bootstrap-profile plus trusted execution-environment resolver-policy
branch, and it invents no resolver-policy semantics. No network, model, provider, git, or
subprocess access occurs outside pytest ``tmp_path``.
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest
import yaml

from converge_orchestrator.config import (
    load_config,
    load_run_config_snapshot,
    materialize_run_config_snapshot,
)
from converge_orchestrator.graph import build_graph as build_legacy_graph
from converge_orchestrator.graph_service import build_graph as build_service_graph

_REQUIREMENTS_TEXT = "The orchestrator converges the repository against immutable requirements.\n"

# Static selection flavors: ``cloud``/``local`` use models.mode + models.profile_sets;
# ``explicit`` uses the equivalent explicit models.profiles form (no selection machinery).
_FLAVORS = ("cloud", "local", "explicit")
_ROLES = ("planner", "builder")

# Canonical lifecycle spine present in both compiled graphs (mirrors the REQ-CONFIG-003 lock).
_LIFECYCLE_NODES = frozenset(
    {
        "__start__", "__end__", "bootstrap", "guard_plan", "guard_quality", "spec_stop",
        "pause_plan", "pause_build", "pause_tdd_red_repair", "pause_repair",
        "pause_integrate", "pause_pr", "pause_merge", "scout", "plan", "prepare_worktree",
        "tdd_baseline", "tdd_red_build", "tdd_red_gate", "snapshot_frozen_red", "tdd_human",
        "build", "quality", "review", "repair", "replan", "human", "integrate", "pr",
        "merge", "refresh",
    }
)


def _profile_map(flavor: str) -> dict[str, dict[str, Any]]:
    return {role: {"model": f"{role}-{flavor}", "context_tokens": 100000} for role in _ROLES}


def _base_data(flavor: str) -> dict[str, Any]:
    """One valid converge.yaml mapping pinning a statically selected execution profile."""
    if flavor == "explicit":
        models: dict[str, Any] = {"profiles": _profile_map("explicit")}
    else:
        models = {
            "mode": flavor,
            "profile_sets": {
                "cloud": _profile_map("cloud"),
                "local": _profile_map("local"),
            },
        }
    return {
        "version": 1,
        "project": {
            "name": f"exec-profile-{flavor}",
            "repo_path": "./repository",
            "requirements_path": "./requirements.md",
            "state_dir": "./state",
            "worktree_dir": "./state/worktrees",
            "require_spec_read_only": False,
        },
        "models": models,
        "agents": {
            "planner": {"agent": "converge-planner", "model_profile": "planner"},
            "builder": {"agent": "converge-builder", "model_profile": "builder"},
        },
    }


def _write_config(base: Path, flavor: str) -> Path:
    base.mkdir(parents=True, exist_ok=True)
    (base / "repository").mkdir(parents=True, exist_ok=True)
    (base / "requirements.md").write_text(_REQUIREMENTS_TEXT, encoding="utf-8")
    config_path = base / "converge.yaml"
    config_path.write_text(yaml.safe_dump(_base_data(flavor), sort_keys=True), encoding="utf-8")
    return config_path


def _selected_models(flavor: str) -> set[str]:
    return {f"{role}-{flavor}" for role in _ROLES}


def _compiled_topology(
    builder: Callable[[], Any],
) -> tuple[frozenset[str], frozenset[tuple[str, str]]]:
    graph = builder().get_graph()
    nodes = frozenset(graph.nodes)
    edges = frozenset((edge.source, edge.target) for edge in graph.edges)
    return nodes, edges


@pytest.mark.parametrize("flavor", _FLAVORS)
def test_execution_profile_loads_only_selected_profiles(tmp_path: Path, flavor: str) -> None:
    """Each statically pinned execution profile loads and exposes only its selected set."""
    cfg = load_config(_write_config(tmp_path / flavor, flavor))

    assert set(cfg.model_profiles) == set(_ROLES)
    exposed = {profile.model for profile in cfg.model_profiles.values()}
    # The unselected set's model identities must not leak into the active configuration.
    assert exposed == _selected_models(flavor)


@pytest.mark.parametrize("flavor", _FLAVORS)
def test_run_snapshot_pins_only_selected_profiles(tmp_path: Path, flavor: str) -> None:
    """The run snapshot pins only the selected profiles and reloads them identically."""
    config_path = _write_config(tmp_path / flavor, flavor)
    cfg, snapshot, digest = materialize_run_config_snapshot(config_path, f"run-{flavor}")

    assert len(digest) == 64
    assert cfg.model_profiles["planner"].model == f"planner-{flavor}"

    raw = yaml.safe_load(snapshot.read_text(encoding="utf-8"))
    # The snapshot is normalized: only the selected profiles remain, no selection machinery.
    assert "mode" not in raw["models"]
    assert "profile_sets" not in raw["models"]
    assert set(raw["models"]["profiles"]) == set(_ROLES)
    assert raw["models"]["profiles"]["planner"]["model"] == f"planner-{flavor}"
    assert raw["models"]["profiles"]["builder"]["model"] == f"builder-{flavor}"

    reloaded = load_run_config_snapshot(snapshot, digest)
    assert reloaded.model_profiles["planner"].model == f"planner-{flavor}"
    assert reloaded.model_profiles["builder"].model == f"builder-{flavor}"


def test_graph_topology_invariant_across_execution_profiles(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Cloud and local execution profiles compile to identical node and edge sets."""
    cloud_config = _write_config(tmp_path / "cloud", "cloud")
    local_config = _write_config(tmp_path / "local", "local")
    cloud_dir, local_dir = cloud_config.parent, local_config.parent

    # The two execution-profile configurations genuinely differ in their selected profiles,
    # so the topology comparison below cannot pass vacuously.
    cloud_cfg = load_config(cloud_config)
    local_cfg = load_config(local_config)
    for role in _ROLES:
        assert cloud_cfg.model_profiles[role].model != local_cfg.model_profiles[role].model

    for label, builder in (
        ("graph.build_graph", build_legacy_graph),
        ("graph_service.build_graph", build_service_graph),
    ):
        # Each variant drives its own compilation: the working directory is pointed at that
        # variant's project root (which holds its converge.yaml), so a builder that derived
        # topology from configuration resolved against the working directory would observe a
        # different configuration on each pass. Identical node and edge sets across the two
        # variants therefore prove the execution-profile choice cannot change topology.
        monkeypatch.chdir(cloud_dir)
        nodes_cloud, edges_cloud = _compiled_topology(builder)
        monkeypatch.chdir(local_dir)
        nodes_local, edges_local = _compiled_topology(builder)

        assert nodes_cloud == nodes_local, f"{label}: node set depends on execution profile"
        assert edges_cloud == edges_local, f"{label}: edge set depends on execution profile"
        assert _LIFECYCLE_NODES <= nodes_cloud, (
            f"{label}: missing canonical lifecycle nodes: "
            f"{sorted(_LIFECYCLE_NODES - nodes_cloud)}"
        )
        assert ("scout", "plan") in edges_cloud, f"{label}: scout -> plan connectivity lost"
