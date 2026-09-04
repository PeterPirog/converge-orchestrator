from __future__ import annotations

from converge_orchestrator.node_compat import node_export_surface


def test_node_export_surface_parses_direct_typescript_exports() -> None:
    surface = node_export_surface(
        "src/index.ts",
        """
export function charge(amount: number): string { return String(amount); }
export interface Receipt { id: string }
export type Currency = "USD" | "EUR";
export const version = "1.0", stable = true;
export default class Client {}
""",
    )

    assert surface is not None
    assert surface.complete is True
    assert surface.symbols == frozenset(
        {"charge", "Receipt", "Currency", "version", "stable", "default"}
    )


def test_node_export_surface_uses_public_alias_from_named_export() -> None:
    surface = node_export_surface(
        "src/index.ts",
        'const internal = 1; export { internal as publicName };\n',
    )

    assert surface is not None
    assert surface.complete is True
    assert surface.symbols == frozenset({"publicName"})


def test_node_export_surface_parses_explicit_reexport_names() -> None:
    surface = node_export_surface(
        "src/index.ts",
        'export { charge, Receipt as PublicReceipt } from "./api";\n',
    )

    assert surface is not None
    assert surface.complete is True
    assert surface.symbols == frozenset({"charge", "PublicReceipt"})


def test_node_export_surface_marks_wildcard_reexport_incomplete() -> None:
    surface = node_export_surface("src/index.ts", 'export * from "./api";\n')

    assert surface is not None
    assert surface.complete is False
    assert surface.symbols == frozenset()


def test_node_export_surface_marks_destructured_export_incomplete() -> None:
    surface = node_export_surface(
        "src/index.ts",
        "export const {left, right} = pair;\n",
    )

    assert surface is not None
    assert surface.complete is False


def test_node_export_surface_uses_tsx_grammar() -> None:
    surface = node_export_surface(
        "src/view.tsx",
        "export function View() { return <div>ok</div>; }\n",
    )

    assert surface is not None
    assert surface.complete is True
    assert surface.symbols == frozenset({"View"})


def test_node_export_surface_returns_incomplete_on_parse_error() -> None:
    surface = node_export_surface("src/index.ts", "export function broken( {\n")

    assert surface is not None
    assert surface.complete is False


def test_node_export_surface_ignores_non_node_source_paths() -> None:
    assert node_export_surface("README.md", "export const value = 1;\n") is None
