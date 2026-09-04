from __future__ import annotations

from converge_orchestrator.node_compat import resolve_node_export_surface


def _reader(sources: dict[str, str]):
    return lambda path: sources.get(path)


def test_resolves_unique_extensionless_local_wildcard() -> None:
    sources = {
        "src/index.ts": 'export * from "./api";\n',
        "src/api.ts": (
            "export function charge(amount: number): void {}\n"
            "export const version = '1';\n"
            "export default class Client {}\n"
        ),
    }

    surface = resolve_node_export_surface(
        "src/index.ts",
        _reader(sources),
        package_root=".",
    )

    assert surface is not None
    assert surface.complete is True
    assert surface.symbols == frozenset({"charge", "version"})
    assert dict(surface.minimum_arguments) == {"charge": 1}
    assert surface.source_paths == frozenset({"src/index.ts", "src/api.ts"})


def test_resolves_transitive_parent_relative_reexport_inside_package() -> None:
    sources = {
        "src/index.ts": 'export * from "./internal/api";\n',
        "src/internal/api.ts": 'export * from "../core";\n',
        "src/core.ts": "export const stable = true;\n",
    }

    surface = resolve_node_export_surface(
        "src/index.ts",
        _reader(sources),
        package_root="src",
    )

    assert surface is not None
    assert surface.complete is True
    assert surface.symbols == frozenset({"stable"})
    assert surface.source_paths == frozenset(
        {"src/index.ts", "src/internal/api.ts", "src/core.ts"}
    )


def test_reexport_resolution_stops_at_depth_budget() -> None:
    sources = {
        "src/index.ts": 'export * from "./a";\n',
        "src/a.ts": 'export * from "./b";\n',
        "src/b.ts": "export const value = 1;\n",
    }

    surface = resolve_node_export_surface(
        "src/index.ts",
        _reader(sources),
        package_root="src",
        max_depth=1,
    )

    assert surface is not None
    assert surface.complete is False


def test_reexport_resolution_stops_at_module_budget() -> None:
    sources = {
        "src/index.ts": 'export * from "./a";\n',
        "src/a.ts": 'export * from "./b";\n',
        "src/b.ts": "export const value = 1;\n",
    }

    surface = resolve_node_export_surface(
        "src/index.ts",
        _reader(sources),
        package_root="src",
        max_modules=2,
    )

    assert surface is not None
    assert surface.complete is False


def test_reexport_resolution_stops_at_edge_budget() -> None:
    sources = {
        "src/index.ts": 'export * from "./a";\nexport * from "./b";\n',
        "src/a.ts": "export const first = 1;\n",
        "src/b.ts": "export const second = 2;\n",
    }

    surface = resolve_node_export_surface(
        "src/index.ts",
        _reader(sources),
        package_root="src",
        max_edges=1,
    )

    assert surface is not None
    assert surface.complete is False


def test_reexport_resolution_marks_cycle_incomplete() -> None:
    sources = {
        "src/index.ts": 'export * from "./a";\n',
        "src/a.ts": 'export * from "./index";\n',
    }

    surface = resolve_node_export_surface(
        "src/index.ts",
        _reader(sources),
        package_root="src",
    )

    assert surface is not None
    assert surface.complete is False


def test_colliding_wildcard_exports_remain_incomplete() -> None:
    sources = {
        "src/index.ts": 'export * from "./a";\nexport * from "./b";\n',
        "src/a.ts": "export const shared = 1;\n",
        "src/b.ts": "export const shared = 2;\n",
    }

    surface = resolve_node_export_surface(
        "src/index.ts",
        _reader(sources),
        package_root="src",
    )

    assert surface is not None
    assert surface.complete is False
    assert "shared" not in surface.symbols


def test_extensionless_reexport_with_multiple_supported_targets_is_ambiguous() -> None:
    sources = {
        "src/index.ts": 'export * from "./api";\n',
        "src/api.ts": "export const typed = true;\n",
        "src/api.js": "export const runtime = true;\n",
    }

    surface = resolve_node_export_surface(
        "src/index.ts",
        _reader(sources),
        package_root="src",
    )

    assert surface is not None
    assert surface.complete is False


def test_reexport_cannot_escape_package_root() -> None:
    sources = {
        "pkg/src/index.ts": 'export * from "../../outside";\n',
        "outside.ts": "export const secret = true;\n",
    }

    surface = resolve_node_export_surface(
        "pkg/src/index.ts",
        _reader(sources),
        package_root="pkg",
    )

    assert surface is not None
    assert surface.complete is False
    assert "outside.ts" not in surface.source_paths
