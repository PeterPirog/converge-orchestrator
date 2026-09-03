from __future__ import annotations

import ast
import difflib
import re
from pathlib import Path, PurePosixPath
from typing import Literal

from pydantic import BaseModel, Field

from .git import changed_files
from .models import ProjectConfig, TaskEnvelope
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
    r"(?i)\b(?:auth(?:enticate|orize|entication|orization)?|permissions?|roles?|scopes?|jwt|oauth|"
    r"oidc|sessions?|tokens?|password|credential|principal|identity|policy|access[_ -]?control)\b"
)
_AUTH_WEAKENING = re.compile(
    r"(?i)(?:verify\s*=\s*false|skip[_ -]?auth|bypass[_ -]?auth|allow[_ -]?anonymous|"
    r"permit[_ -]?all|disable[_ -]?(?:auth|verification)|insecure)"
)

_TEST_SEGMENTS = {"test", "tests", "spec", "specs", "__tests__"}


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
    return bool(set(parts).intersection(_AUTH_SEGMENTS) or stem_tokens.intersection(_AUTH_SEGMENTS))


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
        terms.update(_canonical_auth_term(match.group(0)) for match in _AUTH_PRIMITIVE.finditer(line))
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
    weakening = [(line_no, line) for line_no, line in added if _AUTH_WEAKENING.search(line)]
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
            default = ast.dump(node.args.defaults[index - defaults_start], include_attributes=False)
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
            bases = tuple(ast.dump(base, include_attributes=False) for base in node.bases)
            api[node.name] = ("class", bases)
            for member in node.body:
                if not isinstance(member, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    continue
                if member.name.startswith("_") and member.name != "__init__":
                    continue
                api[f"{node.name}.{member.name}"] = ("method", _function_signature(member))
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


def classify_repository_risk(
    config: ProjectConfig,
    cwd: Path,
    task: TaskEnvelope,
) -> RiskReport:
    """Classify final candidate diff without trusting Planner-provided risk flags."""
    del task  # Reserved for future task-aware adapters; diff evidence remains authoritative.
    findings: list[RiskFinding] = []
    for path in changed_files(cwd, config.base_branch):
        base = _read_base(cwd, config.base_branch, path)
        candidate = _read_candidate(cwd, path)
        added, removed = _changed_line_sets(base, candidate)
        findings.extend(_secret_findings(path, added))
        findings.extend(_migration_findings(path, added, removed, candidate))
        findings.extend(_auth_findings(path, added, removed))
        findings.extend(_python_public_api_findings(path, base, candidate))

    deduped: list[RiskFinding] = []
    seen: set[tuple] = set()
    for finding in findings:
        key = (finding.kind, finding.flag, finding.path, finding.line, finding.evidence)
        if key in seen:
            continue
        seen.add(key)
        deduped.append(finding)

    flags = sorted({finding.flag for finding in deduped if finding.flag})
    return RiskReport(findings=deduped, flags=flags)
