from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from pathlib import PurePosixPath

import tree_sitter_typescript as ts_typescript
from tree_sitter import Language, Node, Parser

_SUPPORTED_SUFFIXES = {".js", ".jsx", ".mjs", ".cjs", ".ts", ".tsx", ".mts", ".cts"}
_TYPESCRIPT_SUFFIXES = (".ts", ".tsx", ".mts", ".cts")
_NODE_RESOLUTION_SUFFIXES = (
    ".d.ts",
    ".ts",
    ".tsx",
    ".mts",
    ".cts",
    ".js",
    ".jsx",
    ".mjs",
    ".cjs",
)
_VARIABLE_DECLARATIONS = {"lexical_declaration", "variable_declaration", "using_declaration"}
_SIMPLE_NAME_TYPES = {"identifier", "type_identifier", "property_identifier"}
_CALLABLE_DECLARATIONS = {
    "function_declaration",
    "function_signature",
    "generator_function_declaration",
}
_DEFAULT_REEXPORT_MAX_DEPTH = 4
_DEFAULT_REEXPORT_MAX_MODULES = 32
_DEFAULT_REEXPORT_MAX_EDGES = 64


@dataclass(frozen=True)
class NodeExportSurface:
    """Syntactic export surface for one JavaScript/TypeScript module.

    `complete` means the consumer-visible names are fully proven for this module. Direct parsing
    leaves wildcard re-exports incomplete until `resolve_node_export_surface` resolves their local
    module graph.

    `minimum_arguments` contains only callable declarations whose call shape is structurally
    provable. `source_paths` records modules that contributed to a resolved surface so callers can
    bind findings to the actual candidate diff.
    """

    symbols: frozenset[str]
    complete: bool
    minimum_arguments: tuple[tuple[str, int], ...] = ()
    wildcard_reexports: tuple[str, ...] = ()
    local_complete: bool = True
    source_paths: frozenset[str] = frozenset()


def is_node_source_path(path: str) -> bool:
    return PurePosixPath(path).suffix.lower() in _SUPPORTED_SUFFIXES


def is_typescript_source_path(path: str) -> bool:
    return path.lower().endswith(_TYPESCRIPT_SUFFIXES)


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


def _module_specifier(source: bytes, statement: Node) -> str | None:
    source_node = statement.child_by_field_name("source")
    if source_node is None:
        source_node = next(
            (child for child in statement.named_children if child.type == "string"),
            None,
        )
    if source_node is None:
        return None

    raw = _source_text(source, source_node)
    if len(raw) < 2 or raw[0] not in {'"', "'"} or raw[-1] != raw[0]:
        return None
    # Escaped module names require JavaScript string decoding. Do not guess a filesystem target.
    if "\\" in raw:
        return None
    return raw[1:-1]


def _wildcard_reexport_source(source: bytes, statement: Node) -> str | None:
    if any(child.type == "namespace_export" for child in statement.named_children):
        return None
    if not any(child.type == "*" for child in statement.children):
        return None
    return _module_specifier(source, statement)


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
    # a direct named-export comparison.
    return set(), False


def _callable_declarations(declaration: Node) -> list[Node]:
    if declaration.type in _CALLABLE_DECLARATIONS:
        return [declaration]
    if declaration.type != "ambient_declaration":
        return []
    declarations: list[Node] = []
    for child in declaration.named_children:
        declarations.extend(_callable_declarations(child))
    return declarations


def _minimum_argument_count(declaration: Node) -> int | None:
    parameters = declaration.child_by_field_name("parameters")
    if parameters is None or parameters.type != "formal_parameters":
        return None

    positional_index = 0
    minimum = 0
    for parameter in parameters.named_children:
        if parameter.type == "comment":
            continue
        if parameter.type not in {"required_parameter", "optional_parameter"}:
            return None

        pattern = parameter.child_by_field_name("pattern")
        if pattern is not None and pattern.type == "this":
            continue
        if pattern is not None and pattern.type == "rest_pattern":
            continue

        positional_index += 1
        has_default = any(child.type == "=" for child in parameter.children)
        if parameter.type == "required_parameter" and not has_default:
            minimum = positional_index
    return minimum


def _export_statement_minimum_arguments(source: bytes, statement: Node) -> dict[str, int]:
    declaration = statement.child_by_field_name("declaration")
    if declaration is None:
        return {}

    has_default = any(child.type == "default" for child in statement.children)
    minimum_arguments: dict[str, int] = {}
    for callable_declaration in _callable_declarations(declaration):
        minimum = _minimum_argument_count(callable_declaration)
        if minimum is None:
            continue
        if has_default:
            name = "default"
        else:
            name = _simple_name(source, callable_declaration.child_by_field_name("name"))
        if name is None:
            continue
        previous = minimum_arguments.get(name)
        minimum_arguments[name] = minimum if previous is None else min(previous, minimum)
    return minimum_arguments


def node_export_surface(path: str, source_text: str | None) -> NodeExportSurface | None:
    """Parse one JS/TS module without executing target repository code.

    The TypeScript Tree-sitter grammar is a real syntax parser and accepts JavaScript as a subset.
    TSX grammar is used for JSX-bearing extensions. Wildcard re-exports are captured structurally
    but remain incomplete until a bounded local resolver proves their target graph.
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
    source_paths = frozenset({path})
    if root.has_error:
        return NodeExportSurface(
            symbols=frozenset(),
            complete=False,
            local_complete=False,
            source_paths=source_paths,
        )

    symbols: set[str] = set()
    callable_minimums: dict[str, int] = {}
    wildcard_reexports: list[str] = []
    local_complete = True
    for child in root.named_children:
        if child.type != "export_statement":
            continue

        wildcard_source = _wildcard_reexport_source(source, child)
        if wildcard_source is not None:
            wildcard_reexports.append(wildcard_source)
            continue

        names, statement_complete = _export_statement_names(source, child)
        symbols.update(names)
        local_complete = local_complete and statement_complete
        for name, minimum in _export_statement_minimum_arguments(source, child).items():
            previous = callable_minimums.get(name)
            callable_minimums[name] = minimum if previous is None else min(previous, minimum)

    return NodeExportSurface(
        symbols=frozenset(symbols),
        complete=local_complete and not wildcard_reexports,
        minimum_arguments=tuple(sorted(callable_minimums.items())),
        wildcard_reexports=tuple(wildcard_reexports),
        local_complete=local_complete,
        source_paths=source_paths,
    )


def _normalize_repo_path(path: PurePosixPath) -> PurePosixPath | None:
    parts: list[str] = []
    for part in path.parts:
        if part in {"", "."}:
            continue
        if part == "..":
            if not parts:
                return None
            parts.pop()
            continue
        parts.append(part)
    return PurePosixPath(*parts) if parts else PurePosixPath(".")


def _is_within_package(path: PurePosixPath, package_root: PurePosixPath) -> bool:
    try:
        path.relative_to(package_root)
    except ValueError:
        return False
    return True


def _reexport_candidates(
    current_path: str,
    specifier: str,
    package_root: PurePosixPath,
) -> tuple[str, ...]:
    if not specifier.startswith(".") or "\\" in specifier or "?" in specifier or "#" in specifier:
        return ()

    current = _normalize_repo_path(PurePosixPath(current_path))
    if current is None:
        return ()
    raw_target = _normalize_repo_path(current.parent / PurePosixPath(specifier))
    if raw_target is None or not _is_within_package(raw_target, package_root):
        return ()

    raw_text = raw_target.as_posix()
    if raw_target.suffix:
        return (raw_text,) if is_node_source_path(raw_text) else ()

    candidates = [f"{raw_text}{suffix}" for suffix in _NODE_RESOLUTION_SUFFIXES]
    candidates.extend(
        (raw_target / f"index{suffix}").as_posix() for suffix in _NODE_RESOLUTION_SUFFIXES
    )
    return tuple(candidates)


def _resolve_reexport_target(
    current_path: str,
    specifier: str,
    package_root: PurePosixPath,
    source_reader: Callable[[str], str | None],
) -> str | None:
    found: str | None = None
    for candidate in _reexport_candidates(current_path, specifier, package_root):
        if source_reader(candidate) is None:
            continue
        if found is not None:
            # More than one supported local target makes extensionless resolution ambiguous.
            return None
        found = candidate
    return found


def resolve_node_export_surface(
    path: str,
    source_reader: Callable[[str], str | None],
    *,
    package_root: str = ".",
    max_depth: int = _DEFAULT_REEXPORT_MAX_DEPTH,
    max_modules: int = _DEFAULT_REEXPORT_MAX_MODULES,
    max_edges: int = _DEFAULT_REEXPORT_MAX_EDGES,
) -> NodeExportSurface | None:
    """Resolve a bounded graph of local wildcard re-exports.

    Resolution is intentionally narrower than Node/TypeScript module resolution. Only relative paths
    confined to the package root are considered. Extensionless specifiers are accepted only when one
    supported source/index candidate exists. Cycles, ambiguity, external packages and exhausted
    budgets return an incomplete surface instead of guessed compatibility evidence.
    """

    if max_depth < 0 or max_modules < 1 or max_edges < 1:
        raise ValueError("re-export resolution budgets must be positive")

    normalized_root = _normalize_repo_path(PurePosixPath(package_root))
    normalized_path = _normalize_repo_path(PurePosixPath(path))
    if normalized_root is None or normalized_path is None:
        return None
    if not _is_within_package(normalized_path, normalized_root):
        return None

    memo: dict[str, NodeExportSurface] = {}
    active: set[str] = set()
    seen_modules: set[str] = set()
    edge_count = 0

    def incomplete(source_path: str, paths: set[str] | None = None) -> NodeExportSurface:
        return NodeExportSurface(
            symbols=frozenset(),
            complete=False,
            local_complete=False,
            source_paths=frozenset(paths or {source_path}),
        )

    def resolve(current_path: str, depth: int) -> NodeExportSurface | None:
        nonlocal edge_count

        if depth > max_depth:
            return incomplete(current_path)
        if current_path in memo:
            return memo[current_path]
        if current_path in active:
            return incomplete(current_path)
        if current_path not in seen_modules:
            if len(seen_modules) >= max_modules:
                return incomplete(current_path)
            seen_modules.add(current_path)

        parsed = node_export_surface(current_path, source_reader(current_path))
        if parsed is None:
            return None
        if not parsed.local_complete:
            return parsed
        if not parsed.wildcard_reexports:
            if parsed.complete:
                memo[current_path] = parsed
            return parsed

        active.add(current_path)
        symbols = set(parsed.symbols)
        minimum_arguments = dict(parsed.minimum_arguments)
        source_paths = set(parsed.source_paths)
        wildcard_origins: dict[str, str] = {}
        complete = True

        try:
            for specifier in parsed.wildcard_reexports:
                edge_count += 1
                if edge_count > max_edges:
                    complete = False
                    break

                target_path = _resolve_reexport_target(
                    current_path,
                    specifier,
                    normalized_root,
                    source_reader,
                )
                if target_path is None:
                    complete = False
                    continue

                dependency = resolve(target_path, depth + 1)
                if dependency is None:
                    complete = False
                    source_paths.add(target_path)
                    continue
                source_paths.update(dependency.source_paths)
                if not dependency.complete:
                    complete = False
                    continue

                dependency_minimum = dict(dependency.minimum_arguments)
                for symbol in sorted(dependency.symbols):
                    if symbol == "default" or symbol in parsed.symbols:
                        continue
                    previous_origin = wildcard_origins.get(symbol)
                    if previous_origin is not None and previous_origin != target_path:
                        # Binding identity across two star sources needs semantic module resolution.
                        symbols.discard(symbol)
                        minimum_arguments.pop(symbol, None)
                        complete = False
                        continue

                    wildcard_origins[symbol] = target_path
                    symbols.add(symbol)
                    if symbol in dependency_minimum:
                        minimum_arguments[symbol] = dependency_minimum[symbol]
        finally:
            active.remove(current_path)

        result = NodeExportSurface(
            symbols=frozenset(symbols),
            complete=complete,
            minimum_arguments=tuple(sorted(minimum_arguments.items())),
            wildcard_reexports=parsed.wildcard_reexports,
            local_complete=parsed.local_complete,
            source_paths=frozenset(source_paths),
        )
        if result.complete:
            memo[current_path] = result
        return result

    return resolve(normalized_path.as_posix(), 0)
