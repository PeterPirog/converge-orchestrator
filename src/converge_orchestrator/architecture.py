from __future__ import annotations

import ast
import hashlib
import json
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, Field, ValidationError

from .git import GitError, current_head
from .models import GateResult, ProjectConfig, PythonImportBoundary

_CACHE_VERSION = 1
_MAX_EVIDENCE_ISSUES = 64


class PythonArchitectureIssue(BaseModel):
    rule: str
    path: str
    kind: Literal["forbidden_import", "relative_import", "parse_error", "symlink_source"]
    module: str | None = None
    line: int | None = None
    required: bool = True
    detail: str = ""

    def identity(self) -> tuple[str, str, str, str]:
        return (self.rule, self.path, self.kind, self.module or "")


class PythonArchitectureScan(BaseModel):
    issues: list[PythonArchitectureIssue] = Field(default_factory=list)
    scanned_files: int = 0


class BaselineArchitectureCache(BaseModel):
    version: Literal[1] = _CACHE_VERSION
    base_commit: str
    policy_sha256: str
    scan: PythonArchitectureScan


def architecture_policy_sha256(config: ProjectConfig) -> str:
    payload = [
        rule.model_dump(mode="json") for rule in config.architecture.python_import_rules
    ]
    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _cache_path(config: ProjectConfig) -> Path:
    return config.state_dir / "architecture-baseline.json"


def load_baseline_architecture_cache(
    config: ProjectConfig,
    *,
    base_commit: str,
) -> PythonArchitectureScan | None:
    path = _cache_path(config)
    if not path.is_file():
        return None
    try:
        cache = BaselineArchitectureCache.model_validate_json(path.read_text(encoding="utf-8"))
    except (OSError, ValidationError):
        return None
    if cache.base_commit != base_commit:
        return None
    if cache.policy_sha256 != architecture_policy_sha256(config):
        return None
    return cache.scan


def write_baseline_architecture_cache(
    config: ProjectConfig,
    *,
    base_commit: str,
    scan: PythonArchitectureScan,
) -> None:
    path = _cache_path(config)
    path.parent.mkdir(parents=True, exist_ok=True)
    cache = BaselineArchitectureCache(
        base_commit=base_commit,
        policy_sha256=architecture_policy_sha256(config),
        scan=scan,
    )
    temporary = path.with_suffix(".json.tmp")
    temporary.write_text(cache.model_dump_json(indent=2) + "\n", encoding="utf-8")
    temporary.replace(path)


def _path_matches_source(path: str, source: str) -> bool:
    if source == ".":
        return True
    return path == source or path.startswith(source + "/")


def _matching_rules(
    path: str,
    rules: list[PythonImportBoundary],
) -> list[PythonImportBoundary]:
    return [
        rule
        for rule in rules
        if any(_path_matches_source(path, source) for source in rule.source_paths)
    ]


def _candidate_files(
    root: Path,
    rules: list[PythonImportBoundary],
) -> tuple[set[Path], list[PythonArchitectureIssue]]:
    files: set[Path] = set()
    issues: list[PythonArchitectureIssue] = []
    for rule in rules:
        for source in rule.source_paths:
            target = root if source == "." else root / source
            if target.is_symlink():
                issues.append(
                    PythonArchitectureIssue(
                        rule=rule.name,
                        path=source,
                        kind="symlink_source",
                        required=rule.required,
                        detail="configured architecture source is a symlink",
                    )
                )
                continue
            if target.is_file():
                if target.suffix == ".py":
                    files.add(target)
                continue
            if not target.is_dir():
                continue
            for candidate in target.rglob("*.py"):
                files.add(candidate)
    return files, issues


def _module_matches(module: str, prefix: str) -> bool:
    return module == prefix or module.startswith(prefix + ".")


def _is_allowed(module: str, rule: PythonImportBoundary) -> bool:
    return any(_module_matches(module, prefix) for prefix in rule.allowed_imports)


def _is_forbidden(module: str, rule: PythonImportBoundary) -> bool:
    return any(_module_matches(module, prefix) for prefix in rule.forbidden_imports)


def _imports(tree: ast.AST) -> list[tuple[str, str, int]]:
    imports: list[tuple[str, str, int]] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imports.extend(("absolute", alias.name, node.lineno) for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            if node.level:
                module = "." * node.level + (node.module or "")
                imports.append(("relative", module, node.lineno))
            elif node.module:
                imports.append(("absolute", node.module, node.lineno))
    return imports


def scan_python_architecture(
    root: Path,
    rules: list[PythonImportBoundary],
) -> PythonArchitectureScan:
    files, issues = _candidate_files(root, rules)
    seen = {issue.identity() for issue in issues}
    scanned_files = 0
    for path in sorted(files, key=lambda item: item.as_posix()):
        try:
            relative = path.relative_to(root).as_posix()
        except ValueError:
            continue
        matching = _matching_rules(relative, rules)
        if not matching:
            continue
        if path.is_symlink():
            for rule in matching:
                issue = PythonArchitectureIssue(
                    rule=rule.name,
                    path=relative,
                    kind="symlink_source",
                    required=rule.required,
                    detail="Python source participating in architecture policy is a symlink",
                )
                if issue.identity() not in seen:
                    issues.append(issue)
                    seen.add(issue.identity())
            continue
        scanned_files += 1
        try:
            source = path.read_text(encoding="utf-8")
            tree = ast.parse(source, filename=relative)
        except (OSError, UnicodeError, SyntaxError) as exc:
            line = exc.lineno if isinstance(exc, SyntaxError) else None
            detail = str(exc).replace("\n", " ")[:300]
            for rule in matching:
                issue = PythonArchitectureIssue(
                    rule=rule.name,
                    path=relative,
                    kind="parse_error",
                    line=line,
                    required=rule.required,
                    detail=detail,
                )
                if issue.identity() not in seen:
                    issues.append(issue)
                    seen.add(issue.identity())
            continue

        imports = _imports(tree)
        for rule in matching:
            for import_kind, module, line in imports:
                if import_kind == "relative":
                    if not rule.forbid_relative_imports:
                        continue
                    issue = PythonArchitectureIssue(
                        rule=rule.name,
                        path=relative,
                        kind="relative_import",
                        module=module,
                        line=line,
                        required=rule.required,
                        detail=f"relative import {module!r} is forbidden by boundary {rule.name!r}",
                    )
                else:
                    if _is_allowed(module, rule) or not _is_forbidden(module, rule):
                        continue
                    issue = PythonArchitectureIssue(
                        rule=rule.name,
                        path=relative,
                        kind="forbidden_import",
                        module=module,
                        line=line,
                        required=rule.required,
                        detail=f"import {module!r} crosses boundary {rule.name!r}",
                    )
                if issue.identity() not in seen:
                    issues.append(issue)
                    seen.add(issue.identity())
    issues.sort(key=lambda item: item.identity())
    return PythonArchitectureScan(issues=issues, scanned_files=scanned_files)


def _baseline_scan(config: ProjectConfig) -> tuple[PythonArchitectureScan, bool, str | None]:
    rules = config.architecture.python_import_rules
    try:
        base_commit = current_head(config.repo_path)
    except GitError:
        return scan_python_architecture(config.repo_path, rules), False, None
    cached = load_baseline_architecture_cache(config, base_commit=base_commit)
    if cached is not None:
        return cached, True, base_commit
    scan = scan_python_architecture(config.repo_path, rules)
    write_baseline_architecture_cache(config, base_commit=base_commit, scan=scan)
    return scan, False, base_commit


def run_architecture_gate(config: ProjectConfig, cwd: Path) -> GateResult | None:
    rules = config.architecture.python_import_rules
    if not rules:
        return None

    baseline, cache_hit, base_commit = _baseline_scan(config)
    candidate = scan_python_architecture(cwd, rules)
    baseline_by_key = {issue.identity(): issue for issue in baseline.issues}
    candidate_by_key = {issue.identity(): issue for issue in candidate.issues}
    new_issues = [
        issue for key, issue in candidate_by_key.items() if key not in baseline_by_key
    ]
    resolved = [key for key in baseline_by_key if key not in candidate_by_key]
    blocking = [issue for issue in new_issues if issue.required]
    evidence = {
        "mode": "monotonic_python_import_boundaries",
        "policy_sha256": architecture_policy_sha256(config),
        "base_commit": base_commit,
        "baseline_cache_hit": cache_hit,
        "baseline_scanned_files": baseline.scanned_files,
        "candidate_scanned_files": candidate.scanned_files,
        "baseline_issue_count": len(baseline.issues),
        "candidate_issue_count": len(candidate.issues),
        "new_issue_count": len(new_issues),
        "new_required_issue_count": len(blocking),
        "resolved_issue_count": len(resolved),
        "new_issues_truncated": len(new_issues) > _MAX_EVIDENCE_ISSUES,
        "new_issues": [
            issue.model_dump(mode="json") for issue in new_issues[:_MAX_EVIDENCE_ISSUES]
        ],
    }
    ok = not blocking
    return GateResult(
        name="architecture_imports",
        ok=ok,
        required=True,
        returncode=0 if ok else 1,
        output=json.dumps(evidence, ensure_ascii=False, sort_keys=True),
    )
