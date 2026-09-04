from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
from pathlib import Path
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, Field

from .backup import BackupError, BackupManifest, BackupProject, verify_deployment_backup

_RESTORE_PLAN_VERSION = 1
_PROJECT_ID_RE = re.compile(r"^[A-Za-z0-9_.-]+$")


class RestoreError(RuntimeError):
    pass


class RestoreProjectPlan(BaseModel):
    project_id: str
    config_target: str
    requirements_target: str
    repo_target: str
    state_target: str
    worktree_target: str
    repo_head: str
    workspace_id: str
    state_store_id: str
    blockers: list[str] = Field(default_factory=list)


class RestorePlan(BaseModel):
    version: Literal[1] = _RESTORE_PLAN_VERSION
    backup: str
    backup_manifest_sha256: str
    persistence_backend: Literal["sqlite", "postgres"]
    database_target: str
    projects: list[RestoreProjectPlan]
    blockers: list[str] = Field(default_factory=list)
    ready: bool
    confirmation_token: str


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _stable_hash(payload: dict[str, str]) -> str:
    raw = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


def _canonical_uuid(value: str) -> bool:
    try:
        return str(UUID(value)) == value.lower()
    except (ValueError, AttributeError):
        return False


def _link_like(path: Path) -> bool:
    if path.is_symlink():
        return True
    is_junction = getattr(path, "is_junction", None)
    return bool(is_junction and is_junction())


def _target_path(raw: str, label: str, blockers: list[str]) -> Path | None:
    path = Path(raw).expanduser()
    if not path.is_absolute():
        blockers.append(f"{label} is not an absolute path on this restore host")
        return None

    # Normalize `.`/`..` without resolving links. `Path.resolve(strict=False)` would follow a
    # broken destination symlink and could incorrectly make that link look like an absent target.
    normalized = Path(os.path.abspath(os.fspath(path)))
    for parent in normalized.parents:
        if _link_like(parent):
            blockers.append(f"{label} has a symlinked or junction parent: {parent}")
            break
    return normalized


def _required_project_artifacts(
    root: Path,
    project: BackupProject,
    blockers: list[str],
) -> Path | None:
    project_id = project.project_id
    if _PROJECT_ID_RE.fullmatch(project_id) is None:
        blockers.append("project ID is unsafe for restore paths")
        return None
    project_dir = root / "projects" / project_id
    required = (
        project_dir / "converge.yaml",
        project_dir / "requirements.md",
        project_dir / "repository.bundle",
    )
    for path in required:
        if _link_like(path) or not path.is_file():
            blockers.append(f"backup artifact missing or unsafe: {path.name}")
    state_dir = project_dir / "state"
    if _link_like(state_dir) or not state_dir.is_dir():
        blockers.append("backup state directory is missing or unsafe")
        return project_dir

    marker = state_dir / ".converge-state-id"
    if _link_like(marker) or not marker.is_file():
        blockers.append("backup state-store identity marker is missing or unsafe")
    else:
        try:
            marker_value = marker.read_text(encoding="utf-8").strip()
        except OSError:
            blockers.append("backup state-store identity marker is unreadable")
        else:
            if marker_value != project.state_store_id:
                blockers.append("backup state-store identity does not match manifest")
    return project_dir


def _check_bundle(
    project_dir: Path | None,
    project: BackupProject,
    blockers: list[str],
) -> None:
    if project_dir is None:
        return
    bundle = project_dir / "repository.bundle"
    if not bundle.is_file() or _link_like(bundle):
        return
    executable = shutil.which("git")
    if executable is None:
        blockers.append("git executable is required to validate repository bundle")
        return
    try:
        result = subprocess.run(
            [executable, "bundle", "list-heads", str(bundle)],
            cwd=project_dir,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            timeout=60,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        blockers.append("repository bundle could not be validated")
        return
    if result.returncode != 0:
        blockers.append("repository bundle is invalid")
        return
    heads = {
        line.split(maxsplit=1)[0]
        for line in result.stdout.splitlines()
        if line.strip()
    }
    if project.repo_head not in heads:
        blockers.append("repository bundle does not contain the manifest HEAD")


def _check_backup_project_hashes(
    project_dir: Path | None,
    project: BackupProject,
    blockers: list[str],
) -> None:
    if project_dir is None:
        return
    config = project_dir / "converge.yaml"
    requirements = project_dir / "requirements.md"
    if config.is_file() and not _link_like(config):
        if _sha256(config) != project.config_sha256:
            blockers.append("project configuration hash does not match manifest project metadata")
    if requirements.is_file() and not _link_like(requirements):
        if _sha256(requirements) != project.requirements_sha256:
            blockers.append("requirements hash does not match manifest project metadata")


def _postgres_target_empty(database_url: str) -> bool:
    try:
        import psycopg
    except ImportError as exc:  # pragma: no cover - optional dependency guard
        raise RestoreError(
            "PostgreSQL restore preflight requires `pip install 'converge-orchestrator[postgres]'`"
        ) from exc

    query = """
        SELECT 1
        FROM pg_catalog.pg_class AS c
        JOIN pg_catalog.pg_namespace AS n ON n.oid = c.relnamespace
        WHERE n.nspname NOT IN ('pg_catalog', 'information_schema')
          AND n.nspname NOT LIKE 'pg_toast%'
          AND c.relkind IN ('r', 'p', 'v', 'm', 'S', 'f')
        LIMIT 1
    """
    try:
        with psycopg.connect(database_url, autocommit=True) as db:
            return db.execute(query).fetchone() is None
    except Exception as exc:
        raise RestoreError(
            "PostgreSQL restore target could not be verified as empty"
        ) from exc


def _postgres_target_binding(database_url: str) -> str:
    """Return a secret-free digest binding the plan to the configured PostgreSQL target."""
    try:
        from psycopg.conninfo import conninfo_to_dict
    except ImportError as exc:  # pragma: no cover - optional dependency guard
        raise RestoreError(
            "PostgreSQL restore preflight requires `pip install 'converge-orchestrator[postgres]'`"
        ) from exc

    try:
        parsed = conninfo_to_dict(database_url)
    except Exception as exc:
        raise RestoreError("PostgreSQL restore target identity could not be parsed") from exc

    identity = {
        key: str(parsed.get(key) or "")
        for key in ("host", "hostaddr", "port", "dbname", "user", "service")
    }
    return _stable_hash(identity)


def _database_artifact_blockers(root: Path, manifest: BackupManifest) -> list[str]:
    blockers: list[str] = []
    sqlite_artifact = root / "database" / "control.sqlite"
    postgres_artifact = root / "database" / "postgres.dump"
    expected = (
        sqlite_artifact
        if manifest.persistence_backend == "sqlite"
        else postgres_artifact
    )
    unexpected = (
        postgres_artifact
        if manifest.persistence_backend == "sqlite"
        else sqlite_artifact
    )

    if _link_like(expected) or not expected.is_file():
        blockers.append("backup database artifact does not match declared persistence backend")
    if unexpected.exists() or _link_like(unexpected):
        blockers.append("backup contains a database artifact for a different persistence backend")
    return blockers


def _database_blockers(
    manifest: BackupManifest,
    control_db_path: Path,
    database_url: str | None,
) -> tuple[str, str, list[str]]:
    blockers: list[str] = []
    if manifest.persistence_backend == "sqlite":
        target = _target_path(
            str(control_db_path),
            "SQLite control database target",
            blockers,
        )
        if target is None:
            target = Path(os.path.abspath(os.fspath(control_db_path.expanduser())))
        if database_url:
            blockers.append(
                "backup uses SQLite but CONVERGE_DATABASE_URL selects PostgreSQL on this host"
            )
        if target.exists() or _link_like(target):
            blockers.append("SQLite control database target already exists")
        binding = _stable_hash({"backend": "sqlite", "target": str(target)})
        return str(target), binding, blockers

    if not database_url:
        blockers.append("backup uses PostgreSQL but CONVERGE_DATABASE_URL is not configured")
        return "postgres:unconfigured", "", blockers

    try:
        binding = _postgres_target_binding(database_url)
    except RestoreError as exc:
        blockers.append(str(exc))
        binding = ""
    if shutil.which("pg_restore") is None:
        blockers.append("pg_restore executable is required for PostgreSQL restore")
    try:
        empty = _postgres_target_empty(database_url)
    except RestoreError as exc:
        blockers.append(str(exc))
    else:
        if not empty:
            blockers.append("PostgreSQL restore target contains user relations and is not empty")
    return "postgres:configured", binding, blockers


def _plan_token(payload: dict) -> str:
    raw = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


def plan_deployment_restore(
    backup_root: Path,
    *,
    control_db_path: Path,
    database_url: str | None,
) -> RestorePlan:
    """Verify a backup and prove restore preconditions without writing deployment state."""
    root = backup_root.expanduser().resolve()
    try:
        manifest = verify_deployment_backup(root)
    except BackupError as exc:
        raise RestoreError(str(exc)) from exc

    manifest_path = root / "manifest.json"
    manifest_hash = _sha256(manifest_path)
    blockers = _database_artifact_blockers(root, manifest)
    database_target, database_target_binding, database_blockers = _database_blockers(
        manifest,
        control_db_path,
        database_url,
    )
    blockers.extend(database_blockers)

    project_ids = [project.project_id for project in manifest.projects]
    if not project_ids:
        blockers.append("backup manifest contains no projects")
    elif len(project_ids) != len(set(project_ids)):
        blockers.append("backup manifest contains duplicate project IDs")

    project_plans: list[RestoreProjectPlan] = []
    seen_directory_targets: list[tuple[str, Path]] = []
    for project in manifest.projects:
        project_blockers: list[str] = []
        if not _canonical_uuid(project.workspace_id):
            project_blockers.append("workspace identity in manifest is invalid")
        if not _canonical_uuid(project.state_store_id):
            project_blockers.append("state-store identity in manifest is invalid")

        project_dir = _required_project_artifacts(root, project, project_blockers)
        _check_backup_project_hashes(project_dir, project, project_blockers)
        _check_bundle(project_dir, project, project_blockers)

        config_target = _target_path(
            project.config_source_path,
            "configuration target",
            project_blockers,
        )
        requirements_target = _target_path(
            project.requirements_source_path,
            "requirements target",
            project_blockers,
        )
        repo_target = _target_path(
            project.repo_source_path,
            "repository target",
            project_blockers,
        )
        state_target = _target_path(
            project.state_source_path,
            "state target",
            project_blockers,
        )
        worktree_target = _target_path(
            project.worktree_source_path,
            "worktree target",
            project_blockers,
        )

        for label, path in (
            ("configuration", config_target),
            ("requirements", requirements_target),
            ("repository", repo_target),
            ("state", state_target),
            ("worktree", worktree_target),
        ):
            if path is not None and (path.exists() or _link_like(path)):
                project_blockers.append(f"{label} restore target already exists")

        for label, path in (("repository", repo_target), ("state", state_target)):
            if path is None:
                continue
            for other_label, other_path in seen_directory_targets:
                try:
                    path.relative_to(other_path)
                    overlaps = True
                except ValueError:
                    try:
                        other_path.relative_to(path)
                        overlaps = True
                    except ValueError:
                        overlaps = False
                if overlaps:
                    project_blockers.append(
                        f"{label} target overlaps another project directory target ({other_label})"
                    )
            seen_directory_targets.append((f"{project.project_id}:{label}", path))

        project_plans.append(
            RestoreProjectPlan(
                project_id=project.project_id,
                config_target=(
                    str(config_target)
                    if config_target
                    else project.config_source_path
                ),
                requirements_target=(
                    str(requirements_target)
                    if requirements_target
                    else project.requirements_source_path
                ),
                repo_target=(
                    str(repo_target) if repo_target else project.repo_source_path
                ),
                state_target=(
                    str(state_target) if state_target else project.state_source_path
                ),
                worktree_target=(
                    str(worktree_target)
                    if worktree_target
                    else project.worktree_source_path
                ),
                repo_head=project.repo_head,
                workspace_id=project.workspace_id,
                state_store_id=project.state_store_id,
                blockers=project_blockers,
            )
        )

    all_blockers = [*blockers]
    for project in project_plans:
        all_blockers.extend(
            f"{project.project_id}: {item}"
            for item in project.blockers
        )

    token_payload = {
        "version": _RESTORE_PLAN_VERSION,
        "backup_manifest_sha256": manifest_hash,
        "persistence_backend": manifest.persistence_backend,
        "database_target": database_target,
        "database_target_binding": database_target_binding,
        "projects": [project.model_dump() for project in project_plans],
        "blockers": all_blockers,
    }
    return RestorePlan(
        backup=str(root),
        backup_manifest_sha256=manifest_hash,
        persistence_backend=manifest.persistence_backend,
        database_target=database_target,
        projects=project_plans,
        blockers=all_blockers,
        ready=not all_blockers,
        confirmation_token=_plan_token(token_payload),
    )
