"""REQ-CONFIG-003: project configuration changes without changing LangGraph topology.

REQ-CONFIG-003 (CONVERGE_ORCHESTRATOR_2_0_SOURCE_OF_TRUTH.md:L201
[6. Project configuration and secret sources]): project configuration SHALL permit
model identities, providers, context limits, request options, fallback profiles, role
bindings, and runtime limits to change without changing LangGraph topology.

This deterministic offline test locks that invariant on both canonical builders: two valid
converge.yaml variants differing in all seven facets are each made the ambient project context
for their own compilation and compile to identical node and edge sets with the canonical
lifecycle nodes and the ``scout -> plan`` connectivity; the builders expose no configuration
channel and their composition bodies reference no configuration facet. No network, model,
provider, subprocess, or git access occurs outside pytest tmp_path.
"""

from __future__ import annotations

import inspect
from pathlib import Path
from typing import Any

import pytest
import yaml

from converge_orchestrator.config import load_config
from converge_orchestrator.graph import build_graph as build_legacy_graph
from converge_orchestrator.graph_service import build_graph as build_service_graph

_REQUIREMENTS_TEXT = "The orchestrator converges the repository against immutable requirements.\n"

# Canonical lifecycle spine present in both compiled graphs.
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

# One distinctive symbol per REQ-CONFIG-003 facet (plus the config source); the ``build_graph``
# composition bodies must reference none, proving composition reads no project configuration.
_CONFIG_FACET_TOKENS = (
    "model_profiles", "model_gateway", "provider_id", "context_tokens", "output_tokens",
    "context_input_fraction", "context_output_reserve_tokens", "request_body",
    "provider_retries", "timeout_seconds", "fallback_model_profiles", "agents",
    "max_repair_attempts", "max_replans", "max_iterations", "max_diff_lines_hard",
    "run_budget", "load_config", "config_path",
)


def _variant_data(flavor: str) -> dict[str, Any]:
    """One valid converge.yaml mapping; all seven REQ-CONFIG-003 facets differ per flavor."""
    alpha = flavor == "alpha"

    def pick(alpha_value: Any, beta_value: Any) -> Any:
        return alpha_value if alpha else beta_value

    return {
        "version": 1,
        "project": {
            "name": f"topology-{flavor}",
            "repo_path": "./repository",
            "requirements_path": "./requirements.md",
            "state_dir": "./state",
            "worktree_dir": "./state/worktrees",
            "require_spec_read_only": False,
        },
        "models": {
            "gateway": {
                "kind": "existing",
                "provider_id": pick("gateway-alpha", "gateway-beta"),  # providers
                "name": pick("Alpha Gateway", "Beta Gateway"),
            },
            "profiles": {
                "scout": {
                    "model": pick("alpha-scout-1", "beta-scout-1"),  # model identities
                    "provider": pick("alpha-provider", "beta-provider"),  # providers
                    "context_tokens": pick(131072, 65536),  # context limits
                    "output_tokens": pick(8192, 4096),  # context limits
                    "request_body": {"temperature": pick(0.2, 0.9)},  # request options
                },
                "planner": {
                    "model": pick("alpha-plan-1", "beta-plan-1"),
                    "provider": pick("alpha-provider", "beta-provider"),
                    "context_tokens": pick(131072, 65536),
                    "output_tokens": pick(8192, 4096),
                    "request_body": {"temperature": pick(0.3, 0.7)},
                },
                "builder": {
                    "model": pick("alpha-build-1", "beta-build-1"),
                    "provider": pick("alpha-provider", "beta-provider"),
                    "context_tokens": pick(262144, 131072),
                    "output_tokens": pick(16384, 32768),
                    "request_body": {"temperature": pick(0.1, 0.4)},
                },
            },
        },
        "agents": {
            "scout": {
                "agent": pick("converge-scout-alpha", "converge-scout-beta"),  # role bindings
                "model_profile": "scout",
                "timeout_seconds": pick(900, 600),  # request options
                "steps": pick(12, 8),  # request options
                "provider_retries": pick(1, 2),  # request options
                "fallback_model_profiles": (["planner"] if alpha else ["builder"]),
            },
            "planner": {
                "agent": pick("converge-planner-alpha", "converge-planner-beta"),
                "model_profile": "planner",
                "timeout_seconds": pick(1800, 900),
                "steps": pick(18, 10),
                "provider_retries": pick(1, 2),
                "fallback_model_profiles": (["builder"] if alpha else ["scout"]),
            },
            "builder": {
                "agent": pick("converge-builder-alpha", "converge-builder-beta"),
                "model_profile": "builder",
                "timeout_seconds": pick(3600, 1800),
                "steps": pick(60, 30),
                "provider_retries": pick(1, 2),
                "fallback_model_profiles": (["planner"] if alpha else ["scout"]),
            },
        },
        "workflow": {
            "max_repair_attempts": pick(3, 5),  # runtime limits
            "max_replans": pick(2, 4),  # runtime limits
            "max_iterations": pick(50, 20),  # runtime limits
            "max_diff_lines_hard": pick(1000, 500),  # runtime limits
            "context_input_fraction": pick(0.70, 0.65),  # context limits
            "context_output_reserve_tokens": pick(4096, 2048),  # context limits
            "run_budget": {  # runtime limits
                "max_wall_time_seconds": pick(86400, 43200),
                "max_model_attempts": pick(512, 256),
                "max_estimated_tokens": pick(8000000, 4000000),
            },
        },
    }


def _write_variant(base: Path, flavor: str) -> Path:
    """Write one valid converge.yaml variant with its repository and requirements file."""
    base.mkdir(parents=True, exist_ok=True)
    (base / "repository").mkdir(parents=True, exist_ok=True)
    (base / "requirements.md").write_text(_REQUIREMENTS_TEXT, encoding="utf-8")
    config_path = base / "converge.yaml"
    config_path.write_text(
        yaml.safe_dump(_variant_data(flavor), sort_keys=True),
        encoding="utf-8",
    )
    return config_path


def _compiled_topology(builder: Any) -> tuple[frozenset[str], frozenset[tuple[str, str]]]:
    graph = builder().get_graph()
    nodes = frozenset(graph.nodes)
    edges = frozenset((edge.source, edge.target) for edge in graph.edges)
    return nodes, edges


def _assert_no_config_facet_in_composition(label: str, builder: Any) -> None:
    source = inspect.getsource(builder)
    hits = sorted(token for token in _CONFIG_FACET_TOKENS if token in source)
    assert not hits, (
        f"{label} composition body references a REQ-CONFIG-003 configuration facet {hits}; "
        "topology must not depend on configuration"
    )


def _assert_seven_facets_differ(cfg_alpha: Any, cfg_beta: Any) -> None:
    a_scout = cfg_alpha.model_profiles["scout"]
    b_scout = cfg_beta.model_profiles["scout"]
    a_builder = cfg_alpha.model_profiles["builder"]
    b_builder = cfg_beta.model_profiles["builder"]

    # Facet 1: model identities
    assert a_scout.model != b_scout.model
    assert a_builder.model != b_builder.model
    # Facet 2: providers
    assert cfg_alpha.model_gateway.provider_id != cfg_beta.model_gateway.provider_id
    assert a_scout.provider != b_scout.provider
    # Facet 3: context limits
    assert a_scout.context_tokens != b_scout.context_tokens
    assert a_builder.output_tokens != b_builder.output_tokens
    assert cfg_alpha.context_input_fraction != cfg_beta.context_input_fraction
    assert cfg_alpha.context_output_reserve_tokens != cfg_beta.context_output_reserve_tokens
    # Facet 4: request options
    assert a_scout.request_body != b_scout.request_body
    assert cfg_alpha.agents["scout"].timeout_seconds != cfg_beta.agents["scout"].timeout_seconds
    assert cfg_alpha.agents["scout"].steps != cfg_beta.agents["scout"].steps
    assert cfg_alpha.agents["scout"].provider_retries != (
        cfg_beta.agents["scout"].provider_retries
    )
    # Facet 5: fallback profiles
    for role in ("scout", "planner", "builder"):
        assert cfg_alpha.agents[role].fallback_model_profiles != (
            cfg_beta.agents[role].fallback_model_profiles
        )
    # Facet 6: role bindings
    for role in ("scout", "planner", "builder"):
        assert cfg_alpha.agents[role].agent != cfg_beta.agents[role].agent
    # Facet 7: runtime limits
    assert cfg_alpha.max_repair_attempts != cfg_beta.max_repair_attempts
    assert cfg_alpha.max_replans != cfg_beta.max_replans
    assert cfg_alpha.max_iterations != cfg_beta.max_iterations
    assert cfg_alpha.max_diff_lines_hard != cfg_beta.max_diff_lines_hard
    assert cfg_alpha.run_budget != cfg_beta.run_budget


def test_config_facets_change_without_changing_langgraph_topology(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    alpha_config_path = _write_variant(tmp_path / "variant-alpha", "alpha")
    beta_config_path = _write_variant(tmp_path / "variant-beta", "beta")
    alpha_dir = alpha_config_path.parent
    beta_dir = beta_config_path.parent

    config_alpha = load_config(alpha_config_path)
    config_beta = load_config(beta_config_path)

    # Both variants are valid, and all seven REQ-CONFIG-003 facets actually took effect
    # with different values, so the topology comparison below cannot pass vacuously.
    _assert_seven_facets_differ(config_alpha, config_beta)

    for label, builder in (
        ("graph.build_graph", build_legacy_graph),
        ("graph_service.build_graph", build_service_graph),
    ):
        # The builder exposes no configuration channel (only the optional checkpointer) and its
        # composition body references no configuration facet, so configuration has no path to
        # influence the compiled topology.
        assert set(inspect.signature(builder).parameters) == {"checkpointer"}, (
            f"{label} exposes a configuration channel in its signature: "
            f"{sorted(inspect.signature(builder).parameters)}"
        )
        _assert_no_config_facet_in_composition(label, builder)

        # Each variant drives its own compilation: the working directory is pointed at that
        # variant's project root (which holds its converge.yaml), so a builder that derived
        # topology from configuration resolved against the working directory would observe a
        # different configuration on each pass. Identical node and edge sets across the two
        # variants therefore prove configuration cannot change topology.
        monkeypatch.chdir(alpha_dir)
        nodes_alpha, edges_alpha = _compiled_topology(builder)
        monkeypatch.chdir(beta_dir)
        nodes_beta, edges_beta = _compiled_topology(builder)

        assert nodes_alpha == nodes_beta, f"{label}: node set depends on configuration"
        assert edges_alpha == edges_beta, f"{label}: edge set depends on configuration"
        assert _LIFECYCLE_NODES <= nodes_alpha, (
            f"{label}: missing canonical lifecycle nodes: "
            f"{sorted(_LIFECYCLE_NODES - nodes_alpha)}"
        )
        assert ("scout", "plan") in edges_alpha, f"{label}: scout -> plan connectivity lost"
