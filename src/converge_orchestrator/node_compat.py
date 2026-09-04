from __future__ import annotations

from dataclasses import dataclass
from pathlib import PurePosixPath

import tree_sitter_typescript as ts_typescript
from tree_sitter import Language, Node, Parser

_SUPPORTED_SUFFIXES = {".js", ".jsx", ".mjs", ".cjs", ".ts", ".tsx", ".mts", ".cts"}
_VARIABLE_DECLARATIONS = {"lexical_declaration", "variable_declaration", "using_declaration"}
_SIMPLE_NAME_TYPES = {"identifier", "type_identifier", "property_identifier"}


@dataclass(frozen=True)
class NodeExportSurface:
    """Syntactic top-level export surface for one JavaScript/TypeScript module.

    `complete` is false when the parser encounters syntax whose exported names require module
    resolution or cannot be represented conservatively. Callers must not treat an incomplete surface
    as proof that a public name disappeared.
    """

    symbols: frozenset[str]
    complete: bool


def is_node_source_path(path: str) -> bool:
    return PurePosixPath(path).suffix.lower() in _SUPPORTED_SUFFIXES


def _source_text(source: bytes, node: Node) -> str:
    return source[node.start_byte : node.end_byte].decode("utf-8")


def _simple_name(source: bytes, node: Node | None) -> str | None:
    if node is None or node.type not in _SIMPLE_NAME_TYPES:
        return None
    return _source_text(source, node)


def _declaration_names(source: bytes, declaration: Node) -> tuple[set[str], bool]:
    if declaration.type in _VARIABLE_DECLARATIONS:
        names: set[str] = set()
        complete = True
        for child in declaration.named_children:
            if child.type != "variable_declarator":
                continue
            name = _simple_name(source, child.child_by_field_name("name"))
            if name is None:
                complete = False
            else:
                names.add(name)
        return names, complete and bool(names)

    name = _simple_name(source, declaration.child_by_field_name("name"))
    if name is not None:
        return {name}, True

    if declaration.type == "ambient_declaration":
        names: set[str] = set()
        complete = True
        found = False
        for child in declaration.named_children:
            if child.type in {"statement_block", "property_identifier"}:
                continue
            child_names, child_complete = _declaration_names(source, child)
            if child_names or child_complete:
                found = True
                names.update(child_names)
                complete = complete and child_complete
        return names, complete and found

    return set(), False


def _export_clause_names(source: bytes, clause: Node) -> tuple[set[str], bool]:
    names: set[str] = set()
    complete = True
    for specifier in clause.named_children:
        if specifier.type != "export_specifier":
            continue
        exported = specifier.child_by_field_name("alias") or specifier.child_by_field_name("name")
        name = _simple_name(source, exported)
        if name is None:
            complete = False
        else:
            names.add(name)
    return names, complete


def _export_statement_names(source: bytes, statement: Node) -> tuple[set[str], bool]:
    declaration = statement.child_by_field_name("declaration")
    value = statement.child_by_field_name("value")
    has_default = any(child.type == "default" for child in statement.children)

    if has_default:
        return {"default"}, True
    if value is not None:
        return {"default"}, True
    if declaration is not None:
        return _declaration_names(source, declaration)

    for child in statement.named_children:
        if child.type == "export_clause":
            return _export_clause_names(source, child)
        if child.type == "namespace_export":
            names = [
                _simple_name(source, nested)
                for nested in child.named_children
                if nested.type in _SIMPLE_NAME_TYPES
            ]
            public = {name for name in names if name is not None}
            return public, len(public) == 1

    # `export * from`, TypeScript `export =`, and `export as namespace` require semantics beyond
    # a direct named-export comparison. Mark the surface incomplete instead of guessing.
    return set(), False


def node_export_surface(path: str, source_text: str | None) -> NodeExportSurface | None:
    """Parse one JS/TS module without executing target repository code.

    The TypeScript Tree-sitter grammar is a real syntax parser and accepts JavaScript as a subset.
    TSX grammar is used for JSX-bearing extensions. This adapter intentionally does not resolve
    `export *` graphs or infer runtime types.
    """

    if source_text is None or not is_node_source_path(path):
        return None

    source = source_text.encode("utf-8")
    language_fn = (
        ts_typescript.language_tsx
        if PurePosixPath(path).suffix.lower() in {".tsx", ".jsx"}
        else ts_typescript.language_typescript
    )
    parser = Parser(Language(language_fn()))
    tree = parser.parse(source)
    root = tree.root_node
    if root.has_error:
        return NodeExportSurface(symbols=frozenset(), complete=False)

    symbols: set[str] = set()
    complete = True
    for child in root.named_children:
        if child.type != "export_statement":
            continue
        names, statement_complete = _export_statement_names(source, child)
        symbols.update(names)
        complete = complete and statement_complete

    return NodeExportSurface(symbols=frozenset(symbols), complete=complete)
