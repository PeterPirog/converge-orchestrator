"""ARCH-CTRL-003: lock runtime and provider adapter isolation.

Agent-runtime and model-provider details SHALL be isolated behind replaceable adapters or profiles.
This deterministic offline contract test proves that changing gateway/provider/profile affects only
adapter/profile selection and model reference resolution, leaving role semantics and LangGraph
topology unchanged, and that model-backed execution delegates only through OpenCodeAdapter.
"""

from __future__ import annotations

import ast
import inspect
import re
from pathlib import Path
from typing import Any

import pytest
import yaml

from converge_orchestrator.config import load_config
from converge_orchestrator.graph import build_graph as build_legacy_graph
from converge_orchestrator.graph_service import build_graph as build_service_graph
from converge_orchestrator.opencode_config import build_opencode_config, resolve_profile_model

_REQUIREMENTS_TEXT = "The orchestrator converges the repository against immutable requirements.\n"

# Canonical lifecycle spine present in both compiled graphs (mirrors the REQ-CONFIG-003 lock).
_LIFECYCLE_NODES = frozenset({
    "__start__", "__end__", "bootstrap", "guard_plan", "guard_quality", "spec_stop",
    "pause_plan", "pause_build", "pause_tdd_red_repair", "pause_repair",
    "pause_integrate", "pause_pr", "pause_merge", "scout", "plan",
    "prepare_worktree", "tdd_baseline", "tdd_red_build", "tdd_red_gate",
    "snapshot_frozen_red", "tdd_human", "build", "quality", "review",
    "repair", "replan", "human", "integrate", "pr", "merge", "refresh",
})

# Modules that execute model-backed work must delegate only through OpenCodeAdapter.
_EXECUTION_MODULES = (
    ("workflow", "converge_orchestrator.workflow"),
    ("targeting", "converge_orchestrator.targeting"),
    ("graph", "converge_orchestrator.graph"),
)

# Names that must not appear in execution modules because they belong to catalog selection
# or model/profile translation layers.
_PROHIBITED_IN_EXECUTION = frozenset({
    "gateway_model_ids", "configured_gateway_model_ids",
    "resolve_profile_model", "resolve_agent_model", "resolve_agent_variant",
    "materialize_opencode_config", "runtime_opencode_config",
    "urlopen", "Request", "model_gateway", "model_profiles",
})

# Tokens that prove the graph-builder composition body cannot read adapter/profile/gateway
# selection state, so topology invariance across adapter variants is non-vacuous.
_ADAPTER_SELECTION_TOKENS = (
    "model_profiles", "model_gateway", "provider_id", "base_url", "package",
    "resolve_profile_model", "resolve_agent_model", "resolve_agent_variant",
    "gateway_model_ids", "configured_gateway_model_ids",
    "materialize_opencode_config", "runtime_opencode_config",
    "load_config", "config_path",
)


def _variant_data(flavor: str) -> dict[str, Any]:
    """Return one valid converge.yaml whose adapter/profile facets differ by flavor."""
    alpha = flavor == "alpha"

    def pick(alpha_value: Any, beta_value: Any) -> Any:
        return alpha_value if alpha else beta_value

    return {
        "version": 1,
        "project": {
            "name": f"adapter-{flavor}", "repo_path": "./repository",
            "requirements_path": "./requirements.md", "state_dir": "./state",
            "worktree_dir": "./state/worktrees", "require_spec_read_only": False,
        },
        "models": {
            "gateway": {
                "kind": pick("existing", "openwebui"),
                "provider_id": pick("static", "openwebui"),
                "name": pick("Static", "OpenWebUI"),
                "base_url": pick(None, "http://127.0.0.1:3000/api"),
                "package": pick("none", "@ai-sdk/openai-compatible"),
            },
            "profiles": {
                "primary": {
                    "model": pick("openai/gpt-4o-mini", "ollama/qwen2.5-coder:7b"),
                    "provider": pick("openai", "ollama"),
                    "variant": pick("nightly", "latest"),
                    "request_body": pick({"temperature": 0.1}, {"top_p": 0.9}),
                },
            },
        },
        "agents": {
            "builder": {"agent": "converge-builder", "model_profile": "primary", "steps": 50}
        },
    }


def _write_variant(base: Path, flavor: str) -> Path:
    """Write a variant converge.yaml with the required repository and requirements file."""
    base.mkdir(parents=True, exist_ok=True)
    (base / "repository").mkdir(parents=True, exist_ok=True)
    (base / "requirements.md").write_text(_REQUIREMENTS_TEXT, encoding="utf-8")
    config_path = base / "converge.yaml"
    config_path.write_text(
        yaml.safe_dump(_variant_data(flavor), sort_keys=True),
        encoding="utf-8",
    )
    return config_path


def _semantic_agent_fields(payload: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """Extract the role semantics that must stay invariant across adapter variants."""
    return {
        agent_id: {
            key: override[key]
            for key in ("description", "mode", "prompt", "permission", "steps")
            if key in override
        }
        for agent_id, override in payload.get("agent", {}).items()
    }


def _assert_adapter_facets_differ(cfg_alpha: Any, cfg_beta: Any) -> None:
    assert cfg_alpha.model_gateway.kind != cfg_beta.model_gateway.kind
    assert cfg_alpha.model_gateway.provider_id != cfg_beta.model_gateway.provider_id
    assert cfg_alpha.model_gateway.base_url != cfg_beta.model_gateway.base_url
    assert cfg_alpha.model_gateway.package != cfg_beta.model_gateway.package

    alpha_profile = cfg_alpha.model_profiles["primary"]
    beta_profile = cfg_beta.model_profiles["primary"]
    assert alpha_profile.model != beta_profile.model
    assert alpha_profile.provider != beta_profile.provider
    assert alpha_profile.variant != beta_profile.variant
    assert alpha_profile.request_body != beta_profile.request_body


def test_adapter_variants_differ_only_in_adapter_selection_and_model_reference(
    tmp_path: Path,
) -> None:
    """Changing gateway/provider/profile leaves role semantics unchanged."""
    alpha_config_path = _write_variant(tmp_path / "variant-alpha", "alpha")
    beta_config_path = _write_variant(tmp_path / "variant-beta", "beta")

    cfg_alpha = load_config(alpha_config_path)
    cfg_beta = load_config(beta_config_path)
    alpha_profile = cfg_alpha.model_profiles["primary"]
    beta_profile = cfg_beta.model_profiles["primary"]
    _assert_adapter_facets_differ(cfg_alpha, cfg_beta)

    # Resolved model references differ, but the semantic role fields are identical.
    alpha_model = resolve_profile_model(cfg_alpha, alpha_profile)
    beta_model = resolve_profile_model(cfg_beta, beta_profile)
    assert alpha_model != beta_model, "model reference must differ between adapter variants"

    payload_alpha = build_opencode_config(cfg_alpha)
    payload_beta = build_opencode_config(cfg_beta)

    assert _semantic_agent_fields(payload_alpha) == _semantic_agent_fields(
        payload_beta
    ), "role description, mode, prompt, permission, and steps must be identical"

    alpha_models = {a: o.get("model") for a, o in payload_alpha.get("agent", {}).items()}
    beta_models = {a: o.get("model") for a, o in payload_beta.get("agent", {}).items()}
    assert alpha_models != beta_models
    for agent_id in alpha_models:
        assert alpha_models[agent_id] != beta_models[agent_id], (
            f"agent {agent_id!r} resolved to the same model reference across variants"
        )


def _compiled_topology(
    builder: Any,
) -> tuple[frozenset[str], frozenset[tuple[str, str]]]:
    graph = builder().get_graph()
    nodes = frozenset(graph.nodes)
    edges = frozenset((edge.source, edge.target) for edge in graph.edges)
    return nodes, edges


def test_graph_topology_invariant_across_adapter_variants(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Adapter/profile changes do not alter the compiled LangGraph node or edge sets."""
    alpha_config_path = _write_variant(tmp_path / "variant-alpha", "alpha")
    beta_config_path = _write_variant(tmp_path / "variant-beta", "beta")

    cfg_alpha = load_config(alpha_config_path)
    cfg_beta = load_config(beta_config_path)
    _assert_adapter_facets_differ(cfg_alpha, cfg_beta)

    alpha_dir, beta_dir = alpha_config_path.parent, beta_config_path.parent

    for label, builder in (
        ("graph.build_graph", build_legacy_graph),
        ("graph_service.build_graph", build_service_graph),
    ):
        assert set(inspect.signature(builder).parameters) == {"checkpointer"}, (
            f"{label} exposes a configuration channel: "
            f"{sorted(inspect.signature(builder).parameters)}"
        )
        source = inspect.getsource(builder)
        hits = sorted(token for token in _ADAPTER_SELECTION_TOKENS if token in source)
        assert not hits, (
            f"{label} references adapter/profile/gateway/selection tokens {hits}; "
            "topology must not depend on adapter selection"
        )

        monkeypatch.chdir(alpha_dir)
        nodes_alpha, edges_alpha = _compiled_topology(builder)
        monkeypatch.chdir(beta_dir)
        nodes_beta, edges_beta = _compiled_topology(builder)

        assert nodes_alpha == nodes_beta, f"{label}: node set depends on adapter variant"
        assert edges_alpha == edges_beta, f"{label}: edge set depends on adapter variant"
        assert _LIFECYCLE_NODES <= nodes_alpha, (
            f"{label}: missing canonical lifecycle nodes: "
            f"{sorted(_LIFECYCLE_NODES - nodes_alpha)}"
        )
        assert ("scout", "plan") in edges_alpha, f"{label}: scout -> plan connectivity lost"


def _module_source_file(module_name: str) -> Path:
    mod = __import__(module_name, fromlist=[""])
    return Path(inspect.getfile(mod))


def _attribute_chain(node: ast.AST) -> str | None:
    parts: list[str] = []
    while isinstance(node, ast.Attribute):
        parts.append(node.attr)
        node = node.value
    if isinstance(node, ast.Name):
        parts.append(node.id)
        return ".".join(reversed(parts))
    return None


def _collect_references(tree: ast.AST) -> set[str]:
    refs: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Name):
            refs.add(node.id)
        elif isinstance(node, ast.Attribute):
            chain = _attribute_chain(node)
            if chain:
                refs.add(chain)
                refs.update(chain.split("."))
        elif isinstance(node, ast.Import):
            for alias in node.names:
                refs.add(alias.asname or alias.name)
        elif isinstance(node, ast.ImportFrom):
            if node.module:
                refs.add(node.module)
            for alias in node.names:
                refs.add(alias.asname or alias.name)
    return refs


def _opencode_adapter_invoke_count(tree: ast.AST) -> int:
    count = 0
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "invoke"
            and any(
                isinstance(child, ast.Name) and child.id == "OpenCodeAdapter"
                for child in ast.walk(node.func)
            )
        ):
            count += 1
    return count


def _prohibited_source_hits(source: str, tokens: frozenset[str]) -> set[str]:
    return {
        token
        for token in tokens
        if re.search(r"\b" + re.escape(token) + r"\b", source)
    }


def test_execution_modules_delegate_only_through_opencode_adapter() -> None:
    """workflow, targeting, and graph invoke only OpenCodeAdapter for model-backed work."""
    for label, module_name in _EXECUTION_MODULES:
        path = _module_source_file(module_name)
        source = path.read_text(encoding="utf-8")
        tree = ast.parse(source)
        refs = _collect_references(tree)

        assert "OpenCodeAdapter" in refs, (
            f"{label} does not reference OpenCodeAdapter for model-backed execution"
        )

        opencode_calls = _opencode_adapter_invoke_count(tree)
        assert opencode_calls >= 1, (
            f"{label} has no OpenCodeAdapter.invoke(...) model-backed call sites"
        )

        hits = _PROHIBITED_IN_EXECUTION & refs
        assert not hits, (
            f"{label} contains prohibited catalog/translation AST references: {sorted(hits)}; "
            "adapter/profile details must stay outside execution modules"
        )

        src_hits = _prohibited_source_hits(source, _PROHIBITED_IN_EXECUTION)
        assert not src_hits, (
            f"{label} source contains prohibited catalog/translation identifiers: "
            f"{sorted(src_hits)}; adapter/profile details must stay outside execution modules"
        )


def test_gateway_catalog_and_model_translation_stay_in_adapter_modules() -> None:
    """Live gateway catalog selection lives in model_gateway; model translation in
    opencode_config."""
    # Gateway catalog selection owns the only live HTTP catalog query.
    mg_path = _module_source_file("converge_orchestrator.model_gateway")
    mg_tree = ast.parse(mg_path.read_text(encoding="utf-8"))
    mg_refs = _collect_references(mg_tree)
    assert "urlopen" in mg_refs or "Request" in mg_refs, (
        "model_gateway must own the live gateway catalog query"
    )
    assert "OpenCodeAdapter" not in mg_refs, (
        "model_gateway must not invoke the execution adapter"
    )

    # Model/profile translation lives in opencode_config.
    oc_path = _module_source_file("converge_orchestrator.opencode_config")
    oc_tree = ast.parse(oc_path.read_text(encoding="utf-8"))
    oc_funcs = {
        node.name
        for node in ast.walk(oc_tree)
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
    }
    for name in ("resolve_profile_model", "resolve_agent_model", "resolve_agent_variant"):
        assert name in oc_funcs, (
            f"opencode_config must define the model/profile translation function {name!r}"
        )

    oc_refs = _collect_references(oc_tree)
    gateway_only_names = {"urlopen", "Request", "gateway_model_ids", "configured_gateway_model_ids"}
    assert not (gateway_only_names & oc_refs), (
        "opencode_config must not perform live gateway catalog selection"
    )
