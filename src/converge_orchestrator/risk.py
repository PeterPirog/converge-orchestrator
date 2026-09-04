from __future__ import annotations

import ast
import difflib
import json
import re
from collections.abc import Callable
from pathlib import Path, PurePosixPath
from typing import Literal

from pydantic import BaseModel, Field

from .git import changed_files
from .models import ProjectConfig, TaskEnvelope
from .node_compat import (
    is_typescript_source_path,
    node_export_surface,
    resolve_node_export_surface,
)
from .shell import run

RiskKind = Literal[
    "secret_material",
    "secret_dependency",
    "destructive_migration",
    "public_api_break",
    "auth_security_change",
]
RiskDisposition = Literal["block", "interrupt", "observe"]


class RiskFinding(BaseModel):
    kind: RiskKind
    disposition: RiskDisposition
    flag: str | None = None
    path: str
    line: int | None = None
    evidence: str


class RiskReport(BaseModel):
    findings: list[RiskFinding] = Field(default_factory=list)
    flags: list[str] = Field(default_factory=list)


_SECRET_MATERIAL_PATTERNS = (
    re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH |DSA )?PRIVATE KEY-----"),
    re.compile(r"\bAKIA[0-9A-Z]{16}\b"),
    re.compile(r"\bgh[pousr]_[A-Za-z0-9]{20,}\b"),
    re.compile(r"\bgithub_pat_[A-Za-z0-9_]{20,}\b"),
    re.compile(r"\bxox[baprs]-[A-Za-z0-9-]{20,}\b"),
    re.compile(r"\bsk-[A-Za-z0-9_-]{20,}\b"),
)
_LITERAL_SECRET_ASSIGNMENT = re.compile(
    r"(?i)\b(?P<name>api[_-]?key|access[_-]?token|token|secret|client[_-]?secret|password|passwd)"
    r"\b\s*[:=]\s*[\"'](?P<value>[^\"']{8,})[\"']"
)
_SECRET_DEPENDENCY = re.compile(
    r"\b[A-Z][A-Z0-9_]{2,}_(?:API_KEY|TOKEN|SECRET|PASSWORD|PRIVATE_KEY)\b"
)
_SAFE_LITERAL_HINTS = (
    "example",
    "sample",
    "dummy",
    "changeme",
    "change_me",
    "placeholder",
    "test-only",
    "not-a-secret",
    "${",
    "{{",
)

_MIGRATION_SEGMENTS = {
    "migration",
    "migrations",
    "alembic",
    "flyway",
    "liquibase",
}
_DESTRUCTIVE_MIGRATION = re.compile(
    r"(?i)\b(?:"
    r"drop\s+(?:table|column|database|schema|index)|"
    r"truncate\s+(?:table\s+)?|"
    r"delete\s+from|"
    r"remove_(?:column|field)|"
    r"drop_(?:column|table|constraint)|"
    r"migrations\.Remove(?:Field|Model)|"
    r"op\.drop_(?:column|table|constraint)|"
    r"rename\s+(?:column|table)"
    r")\b"
)

_AUTH_SEGMENTS = {
    "auth",
    "authn",
    "authz",
    "authentication",
    "authorization",
    "security",
    "permissions",
    "permission",
    "acl",
    "oauth",
    "oidc",
    "jwt",
    "sessions",
    "session",
}
_AUTH_PRIMITIVE = re.compile(
    r"(?i)(?<![A-Za-z0-9])(?:auth(?:enticate|orize|entication|orization)?|permissions?|roles?|"
    r"scopes?|jwt|oauth|oidc|sessions?|tokens?|password|credential|principal|identity|policy|"
    r"access[_ -]?control)(?![A-Za-z0-9])"
)
_AUTH_WEAKENING = re.compile(
    r"(?i)(?:verify\s*=\s*false|skip[_ -]?auth|bypass[_ -]?auth|allow[_ -]?anonymous|"
    r"permit[_ -]?all|disable[_ -]?(?:auth|verification)|insecure)"
)

_TEST_SEGMENTS = {"test", "tests", "spec", "specs", "__tests__"}

_NODE_ENTRYPOINT_FIELDS = ("main", "module", "types", "typings", "browser")
_NODE_SOURCE_ENTRY_NAMES = {"main", "module", "types", "typings"}


def _read_candidate(cwd: Path, path: str) -> str | None:
    candidate = cwd / path
    if not candidate.is_file():
        return None
    try:
        return candidate.read_text(encoding="utf-8")
    except (UnicodeDecodeError, OSError):
        return None


def _read_base(cwd: Path, base_branch: str, path: str) -> str | None:
    result = run(
        ["git", "show", f"origin/{base_branch}:{path}"],
        cwd=cwd,
        timeout=60,
    )
    return result.stdout if result.returncode == 0 else None


def _changed_line_sets(
    base: str | None,
    candidate: str | None,
) -> tuple[list[tuple[int, str]], list[str]]:
    base_lines = (base or "").splitlines()
    candidate_lines = (candidate or "").splitlines()
    matcher = difflib.SequenceMatcher(a=base_lines, b=candidate_lines, autojunk=False)
    added: list[tuple[int, str]] = []
    removed: list[str] = []
    for tag, i1, i2, j1, j2 in matcher.get_opcodes():
        if tag in {"replace", "delete"}:
            removed.extend(base_lines[i1:i2])
        if tag in {"replace", "insert"}:
            added.extend((index + 1, candidate_lines[index]) for index in range(j1, j2))
    return added, removed


def _is_test_path(path: str) -> bool:
    parts = [part.lower() for part in PurePosixPath(path).parts]
    return any(part in _TEST_SEGMENTS for part in parts[:-1])


def _migration_path(path: str) -> bool:
    parts = [part.lower() for part in PurePosixPath(path).parts]
    name = PurePosixPath(path).name.lower()
    return (
        any(part in _MIGRATION_SEGMENTS for part in parts)
        or "migration" in name
        or name.startswith("v") and "__" in name and name.endswith(".sql")
    )


def _auth_path(path: str) -> bool:
    normalized = PurePosixPath(path)
    parts = [part.lower() for part in normalized.parts[:-1]]
    stem_tokens = set(re.split(r"[^a-z0-9]+", normalized.stem.lower()))
    return bool(
        set(parts).intersection(_AUTH_SEGMENTS)
        or stem_tokens.intersection(_AUTH_SEGMENTS)
    )


def _canonical_auth_term(raw: str) -> str:
    value = re.sub(r"[_ -]+", "_", raw.lower())
    if value.startswith("auth"):
        return "auth"
    if value in {"permission", "permissions"}:
        return "permission"
    if value in {"role", "roles"}:
        return "role"
    if value in {"scope", "scopes"}:
        return "scope"
    if value in {"session", "sessions"}:
        return "session"
    if value in {"token", "tokens"}:
        return "token"
    return value


def _auth_terms(lines: list[str]) -> set[str]:
    terms: set[str] = set()
    for line in lines:
        terms.update(
            _canonical_auth_term(match.group(0))
            for match in _AUTH_PRIMITIVE.finditer(line)
        )
    return terms


def _redact_secret_evidence(path: str, line: int | None, kind: str) -> str:
    suffix = f" at line {line}" if line else ""
    return f"{kind} detected in {path}{suffix}; value redacted"


def _secret_findings(
    path: str,
    added: list[tuple[int, str]],
) -> list[RiskFinding]:
    findings: list[RiskFinding] = []
    for line_no, line in added:
        material = any(pattern.search(line) for pattern in _SECRET_MATERIAL_PATTERNS)
        assignment = _LITERAL_SECRET_ASSIGNMENT.search(line)
        if assignment:
            value = assignment.group("value").lower()
            if not any(hint in value for hint in _SAFE_LITERAL_HINTS):
                material = True
        if material:
            findings.append(
                RiskFinding(
                    kind="secret_material",
                    disposition="block",
                    flag="secret_material_detected",
                    path=path,
                    line=line_no,
                    evidence=_redact_secret_evidence(path, line_no, "secret-like material"),
                )
            )
            continue
        dependency = _SECRET_DEPENDENCY.search(line)
        if dependency:
            findings.append(
                RiskFinding(
                    kind="secret_dependency",
                    disposition="interrupt",
                    flag="secret_required",
                    path=path,
                    line=line_no,
                    evidence=f"secret dependency identifier {dependency.group(0)} added in {path}",
                )
            )
    return findings


def _migration_findings(
    path: str,
    added: list[tuple[int, str]],
    removed: list[str],
    candidate: str | None,
) -> list[RiskFinding]:
    if not _migration_path(path):
        return []
    if candidate is None:
        return [
            RiskFinding(
                kind="destructive_migration",
                disposition="interrupt",
                flag="destructive_data_migration",
                path=path,
                evidence=f"existing migration file deleted: {path}",
            )
        ]
    match_lines = [
        line_no for line_no, line in added if _DESTRUCTIVE_MIGRATION.search(line)
    ]
    removed_match = any(_DESTRUCTIVE_MIGRATION.search(line) for line in removed)
    findings = [
        RiskFinding(
            kind="destructive_migration",
            disposition="interrupt",
            flag="destructive_data_migration",
            path=path,
            line=line_no,
            evidence=f"destructive migration operation detected in {path}:{line_no}",
        )
        for line_no in match_lines[:5]
    ]
    if removed_match and not findings:
        findings.append(
            RiskFinding(
                kind="destructive_migration",
                disposition="interrupt",
                flag="destructive_data_migration",
                path=path,
                evidence=f"destructive migration semantics removed or rewritten in {path}",
            )
        )
    return findings


def _auth_findings(
    path: str,
    added: list[tuple[int, str]],
    removed: list[str],
) -> list[RiskFinding]:
    if _is_test_path(path) or not _auth_path(path):
        return []
    added_lines = [line for _, line in added]
    added_security = [
        (line_no, line) for line_no, line in added if _AUTH_PRIMITIVE.search(line)
    ]
    removed_security = [line for line in removed if _AUTH_PRIMITIVE.search(line)]
    weakening = [
        (line_no, line) for line_no, line in added if _AUTH_WEAKENING.search(line)
    ]
    lost_terms = _auth_terms(removed_security) - _auth_terms(added_lines)
    changed_security_lines = len(added_security) + len(removed_security)
    if weakening or lost_terms or changed_security_lines >= 8:
        line_no = (
            weakening[0][0]
            if weakening
            else (added_security[0][0] if added_security else None)
        )
        if weakening:
            reason = "explicit auth weakening"
        elif lost_terms:
            reason = "security primitive removed: " + ", ".join(sorted(lost_terms))
        else:
            reason = "authorization/authentication contract changed substantially"
        return [
            RiskFinding(
                kind="auth_security_change",
                disposition="interrupt",
                flag="critical_auth_redesign",
                path=path,
                line=line_no,
                evidence=f"{reason} in security-sensitive path {path}",
            )
        ]
    if changed_security_lines:
        return [
            RiskFinding(
                kind="auth_security_change",
                disposition="observe",
                path=path,
                line=added_security[0][0] if added_security else None,
                evidence=f"auth/security surface changed in {path}",
            )
        ]
    return []


def _annotation(node: ast.expr | None) -> str | None:
    return ast.dump(node, include_attributes=False) if node is not None else None


def _function_signature(node: ast.FunctionDef | ast.AsyncFunctionDef) -> tuple:
    positional = [*node.args.posonlyargs, *node.args.args]
    defaults_start = len(positional) - len(node.args.defaults)
    positional_payload = []
    for index, arg in enumerate(positional):
        default = None
        if index >= defaults_start:
            default = ast.dump(
                node.args.defaults[index - defaults_start],
                include_attributes=False,
            )
        positional_payload.append((arg.arg, _annotation(arg.annotation), default))
    kwonly_payload = [
        (
            arg.arg,
            _annotation(arg.annotation),
            None if default is None else ast.dump(default, include_attributes=False),
        )
        for arg, default in zip(node.args.kwonlyargs, node.args.kw_defaults, strict=True)
    ]
    return (
        tuple(positional_payload),
        tuple(kwonly_payload),
        node.args.vararg.arg if node.args.vararg else None,
        node.args.kwarg.arg if node.args.kwarg else None,
        _annotation(node.returns),
        isinstance(node, ast.AsyncFunctionDef),
    )


def _module_public_api(source: str | None) -> dict[str, tuple]:
    if source is None:
        return {}
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return {}
    explicit_exports: set[str] | None = None
    for node in tree.body:
        if isinstance(node, (ast.Assign, ast.AnnAssign)):
            target = (
                node.targets[0]
                if isinstance(node, ast.Assign) and node.targets
                else node.target
            )
            if isinstance(target, ast.Name) and target.id == "__all__":
                value = node.value
                if isinstance(value, (ast.List, ast.Tuple)) and all(
                    isinstance(item, ast.Constant) and isinstance(item.value, str)
                    for item in value.elts
                ):
                    explicit_exports = {str(item.value) for item in value.elts}
    api: dict[str, tuple] = {}
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            if node.name.startswith("_"):
                continue
            if explicit_exports is not None and node.name not in explicit_exports:
                continue
            api[node.name] = ("function", _function_signature(node))
        elif isinstance(node, ast.ClassDef):
            if node.name.startswith("_"):
                continue
            if explicit_exports is not None and node.name not in explicit_exports:
                continue
            bases = tuple(
                ast.dump(base, include_attributes=False) for base in node.bases
            )
            api[node.name] = ("class", bases)
            for member in node.body:
                if not isinstance(member, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    continue
                if member.name.startswith("_") and member.name != "__init__":
                    continue
                api[f"{node.name}.{member.name}"] = (
                    "method",
                    _function_signature(member),
                )
    return api


def _python_public_api_findings(
    path: str,
    base: str | None,
    candidate: str | None,
) -> list[RiskFinding]:
    normalized = PurePosixPath(path)
    if normalized.suffix != ".py" or _is_test_path(path):
        return []
    if normalized.name.startswith("_") and normalized.name != "__init__.py":
        return []
    baseline_api = _module_public_api(base)
    if not baseline_api:
        return []
    candidate_api = _module_public_api(candidate)
    findings: list[RiskFinding] = []
    for symbol, signature in baseline_api.items():
        if symbol not in candidate_api:
            findings.append(
                RiskFinding(
                    kind="public_api_break",
                    disposition="interrupt",
                    flag="forbidden_public_api_change",
                    path=path,
                    evidence=f"public Python symbol removed: {path}:{symbol}",
                )
            )
        elif candidate_api[symbol] != signature:
            findings.append(
                RiskFinding(
                    kind="public_api_break",
                    disposition="interrupt",
                    flag="forbidden_public_api_change",
                    path=path,
                    evidence=f"public Python signature changed: {path}:{symbol}",
                )
            )
    return findings[:20]


def _flatten_node_export_target(
    contract: dict[str, str],
    entry: str,
    target: object,
) -> None:
    if target is None:
        return
    if isinstance(target, dict):
        for condition, nested in target.items():
            if isinstance(condition, str):
                _flatten_node_export_target(contract, f"{entry}#{condition}", nested)
        return
    contract[entry] = json.dumps(target, sort_keys=True, separators=(",", ":"))


def _node_package_contract(source: str | None) -> dict[str, str]:
    """Return only the stable, consumer-visible contract declared by package.json.

    The manifest is parsed as JSON instead of trying to infer JavaScript/TypeScript exports with a
    lossy regex parser. Additive entries are compatible; existing public names are pinned while
    target changes remain review evidence instead of automatic HITL.
    """
    if source is None:
        return {}
    try:
        payload = json.loads(source)
    except (json.JSONDecodeError, TypeError):
        return {}
    if not isinstance(payload, dict):
        return {}

    contract: dict[str, str] = {}
    name = payload.get("name")
    if isinstance(name, str):
        contract["name"] = name

    for field in _NODE_ENTRYPOINT_FIELDS:
        value = payload.get(field)
        if isinstance(value, str) or isinstance(value, dict):
            contract[field] = json.dumps(value, sort_keys=True, separators=(",", ":"))

    exports = payload.get("exports")
    if isinstance(exports, str) or isinstance(exports, list):
        _flatten_node_export_target(contract, "exports:.", exports)
    elif isinstance(exports, dict):
        # A mapping whose keys start with '.' declares subpath exports. Otherwise the whole object
        # is the conditional root export (for example import/require/default).
        if any(isinstance(key, str) and key.startswith(".") for key in exports):
            for key, value in exports.items():
                if isinstance(key, str) and key.startswith("."):
                    _flatten_node_export_target(contract, f"exports:{key}", value)
        else:
            _flatten_node_export_target(contract, "exports:.", exports)

    binary = payload.get("bin")
    if isinstance(binary, str):
        command = name if isinstance(name, str) else "<package-name>"
        contract[f"bin:{command}"] = binary
    elif isinstance(binary, dict):
        for command, target in binary.items():
            if isinstance(command, str) and isinstance(target, str):
                contract[f"bin:{command}"] = target
    return contract


def _node_package_contract_findings(
    path: str,
    base: str | None,
    candidate: str | None,
) -> list[RiskFinding]:
    if PurePosixPath(path).name != "package.json":
        return []
    baseline = _node_package_contract(base)
    if not baseline:
        return []
    current = _node_package_contract(candidate)
    findings: list[RiskFinding] = []
    for entry, target in baseline.items():
        if entry not in current:
            findings.append(
                RiskFinding(
                    kind="public_api_break",
                    disposition="interrupt",
                    flag="forbidden_public_api_change",
                    path=path,
                    evidence=f"public Node package contract removed: {path}:{entry}",
                )
            )
        elif current[entry] != target:
            if entry == "name":
                findings.append(
                    RiskFinding(
                        kind="public_api_break",
                        disposition="interrupt",
                        flag="forbidden_public_api_change",
                        path=path,
                        evidence=f"public Node package name changed: {path}:name",
                    )
                )
            else:
                findings.append(
                    RiskFinding(
                        kind="public_api_break",
                        disposition="observe",
                        path=path,
                        evidence=f"public Node package target changed: {path}:{entry}",
                    )
                )
    return findings[:20]


def _node_manifest_candidates(paths: list[str]) -> list[str]:
    manifests: set[str] = set()
    for path in paths:
        parent = PurePosixPath(path).parent
        while True:
            manifest = (
                PurePosixPath("package.json")
                if parent == PurePosixPath(".")
                else parent / "package.json"
            )
            manifests.add(manifest.as_posix())
            if parent == PurePosixPath("."):
                break
            parent = parent.parent
    return sorted(manifests)


def _node_local_targets(entry: str, serialized: str) -> set[str]:
    if entry == "name":
        return set()
    if entry.startswith("bin:"):
        value: object = serialized
    else:
        try:
            value = json.loads(serialized)
        except (json.JSONDecodeError, TypeError):
            return set()

    targets: set[str] = set()

    def collect(item: object) -> None:
        if isinstance(item, str):
            if item.startswith("./") and "*" not in item:
                targets.add(item)
            return
        if isinstance(item, list):
            for nested in item:
                collect(nested)
            return
        if isinstance(item, dict):
            for nested in item.values():
                collect(nested)

    collect(value)
    return targets


def _node_target_path(manifest_path: str, target: str) -> str | None:
    relative = PurePosixPath(target[2:])
    if not relative.parts or any(part in {"", ".."} for part in relative.parts):
        return None
    package_dir = PurePosixPath(manifest_path).parent
    resolved = package_dir / relative
    return resolved.as_posix()


def _node_published_target_findings(
    paths: list[str],
    base_source: Callable[[str], str | None],
    candidate_source: Callable[[str], str | None],
) -> list[RiskFinding]:
    """Block definite deletion of a file still referenced by a pre-existing public Node entry."""
    changed = set(paths)
    findings: list[RiskFinding] = []
    for manifest_path in _node_manifest_candidates(paths):
        candidate_manifest = candidate_source(manifest_path)
        if candidate_manifest is None:
            continue
        baseline = _node_package_contract(base_source(manifest_path))
        current = _node_package_contract(candidate_manifest)
        if not baseline or not current:
            continue
        for entry in sorted(set(baseline).intersection(current)):
            for target in sorted(_node_local_targets(entry, current[entry])):
                target_path = _node_target_path(manifest_path, target)
                if target_path is None or target_path not in changed:
                    continue
                if base_source(target_path) is None or candidate_source(target_path) is not None:
                    continue
                findings.append(
                    RiskFinding(
                        kind="public_api_break",
                        disposition="interrupt",
                        flag="forbidden_public_api_change",
                        path=target_path,
                        evidence=(
                            "public Node package target removed while still published: "
                            f"{manifest_path}:{entry} -> {target}"
                        ),
                    )
                )
                if len(findings) >= 20:
                    return findings
    return findings


def _node_source_entry(entry: str) -> bool:
    return entry in _NODE_SOURCE_ENTRY_NAMES or entry.startswith("exports:")


def _node_package_root(manifest_path: str) -> str:
    return PurePosixPath(manifest_path).parent.as_posix()


def _node_published_source_export_findings(
    paths: list[str],
    base_source: Callable[[str], str | None],
    candidate_source: Callable[[str], str | None],
) -> list[RiskFinding]:
    """Compare proven exports and TypeScript call shapes through bounded local barrels."""
    changed = set(paths)
    findings: list[RiskFinding] = []
    for manifest_path in _node_manifest_candidates(paths):
        baseline = _node_package_contract(base_source(manifest_path))
        current = _node_package_contract(candidate_source(manifest_path))
        if not baseline or not current:
            continue
        for entry in sorted(set(baseline).intersection(current)):
            if not _node_source_entry(entry):
                continue
            baseline_targets = _node_local_targets(entry, baseline[entry])
            current_targets = _node_local_targets(entry, current[entry])
            for target in sorted(baseline_targets.intersection(current_targets)):
                target_path = _node_target_path(manifest_path, target)
                if target_path is None:
                    continue

                baseline_root = node_export_surface(target_path, base_source(target_path))
                candidate_root = node_export_surface(target_path, candidate_source(target_path))
                if target_path not in changed:
                    if baseline_root is None or candidate_root is None:
                        continue
                    if not (
                        baseline_root.wildcard_reexports
                        or candidate_root.wildcard_reexports
                    ):
                        continue

                base_probes: set[str] = set()
                candidate_probes: set[str] = set()

                def traced_base(path: str) -> str | None:
                    base_probes.add(path)
                    return base_source(path)

                def traced_candidate(path: str) -> str | None:
                    candidate_probes.add(path)
                    return candidate_source(path)

                package_root = _node_package_root(manifest_path)
                baseline_surface = resolve_node_export_surface(
                    target_path,
                    traced_base,
                    package_root=package_root,
                )
                candidate_surface = resolve_node_export_surface(
                    target_path,
                    traced_candidate,
                    package_root=package_root,
                )

                surface_paths = base_probes | candidate_probes | {target_path}
                if baseline_surface is not None:
                    surface_paths.update(baseline_surface.source_paths)
                if candidate_surface is not None:
                    surface_paths.update(candidate_surface.source_paths)
                affected_paths = sorted(changed.intersection(surface_paths))
                if not affected_paths:
                    continue
                finding_path = target_path if target_path in changed else affected_paths[0]

                if baseline_surface is None or not baseline_surface.complete:
                    continue
                if candidate_surface is None:
                    # Exact published-target deletion is handled by _node_published_target_findings.
                    continue
                if not candidate_surface.complete:
                    findings.append(
                        RiskFinding(
                            kind="public_api_break",
                            disposition="observe",
                            path=finding_path,
                            evidence=(
                                "public Node source export surface could not be proven "
                                "after change: "
                                f"{manifest_path}:{entry} -> {target}"
                            ),
                        )
                    )
                    continue
                for symbol in sorted(baseline_surface.symbols - candidate_surface.symbols):
                    findings.append(
                        RiskFinding(
                            kind="public_api_break",
                            disposition="interrupt",
                            flag="forbidden_public_api_change",
                            path=finding_path,
                            evidence=(
                                "public Node source export removed: "
                                f"{manifest_path}:{entry} -> {target}:{symbol}"
                            ),
                        )
                    )
                    if len(findings) >= 20:
                        return findings

                if not is_typescript_source_path(target_path):
                    continue
                baseline_minimum = dict(baseline_surface.minimum_arguments)
                candidate_minimum = dict(candidate_surface.minimum_arguments)
                for symbol in sorted(set(baseline_minimum).intersection(candidate_minimum)):
                    before = baseline_minimum[symbol]
                    after = candidate_minimum[symbol]
                    if after <= before:
                        continue
                    findings.append(
                        RiskFinding(
                            kind="public_api_break",
                            disposition="interrupt",
                            flag="forbidden_public_api_change",
                            path=finding_path,
                            evidence=(
                                "public TypeScript callable minimum argument count increased: "
                                f"{manifest_path}:{entry} -> {target}:{symbol} "
                                f"({before} -> {after})"
                            ),
                        )
                    )
                    if len(findings) >= 20:
                        return findings
    return findings


def classify_repository_risk(
    config: ProjectConfig,
    cwd: Path,
    task: TaskEnvelope,
) -> RiskReport:
    """Classify final candidate diff without trusting Planner-provided risk flags."""
    del task  # Reserved for future task-aware adapters; diff evidence remains authoritative.
    findings: list[RiskFinding] = []
    paths = changed_files(cwd, config.base_branch)
    base_cache: dict[str, str | None] = {}
    candidate_cache: dict[str, str | None] = {}

    def base_source(path: str) -> str | None:
        if path not in base_cache:
            base_cache[path] = _read_base(cwd, config.base_branch, path)
        return base_cache[path]

    def candidate_source(path: str) -> str | None:
        if path not in candidate_cache:
            candidate_cache[path] = _read_candidate(cwd, path)
        return candidate_cache[path]

    findings.extend(
        _node_published_target_findings(paths, base_source, candidate_source)
    )
    findings.extend(
        _node_published_source_export_findings(paths, base_source, candidate_source)
    )
    for path in paths:
        base = base_source(path)
        candidate = candidate_source(path)
        added, removed = _changed_line_sets(base, candidate)
        findings.extend(_secret_findings(path, added))
        findings.extend(_migration_findings(path, added, removed, candidate))
        findings.extend(_auth_findings(path, added, removed))
        findings.extend(_python_public_api_findings(path, base, candidate))
        findings.extend(_node_package_contract_findings(path, base, candidate))

    deduped: list[RiskFinding] = []
    seen: set[tuple] = set()
    for finding in findings:
        key = (
            finding.kind,
            finding.flag,
            finding.path,
            finding.line,
            finding.evidence,
        )
        if key in seen:
            continue
        seen.add(key)
        deduped.append(finding)

    flags = sorted({finding.flag for finding in deduped if finding.flag})
    return RiskReport(findings=deduped, flags=flags)
