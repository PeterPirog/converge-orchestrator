from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import sqlite3
import subprocess
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal, Protocol
from uuid import uuid4

from pydantic import BaseModel, Field

from .config import load_config
from .git import GitError, current_head
from .models import ProjectConfig
from .postgres_client import libpq_env
from .shell import run
from .workspace_identity import assert_state_store_affinity, assert_workspace_affinity
from .workspace_ownership import WorkspaceOwnershipStore

_BACKUP_VERSION = 1
_PROJECT_ID_RE = re.compile(r"^[A-Za-z0-9_.-]+$")


class BackupError(RuntimeError):
    pass


class RegistryReader(Protocol):
    def list_projects(self) -> list[dict[str, Any]]: ...

    def runs_for_project(self, project_id: str) -> list[dict[str, Any]]: ...


class BackupFile(BaseModel):
    path: str
    size: int = Field(ge=0)
    sha256: str


class BackupProject(BaseModel):
    project_id: str
    config_source_path: str
    requirements_source_path: str
    repo_source_path: str
    state_source_path: str
    worktree_source_path: str
    workspace_id: str
    state_store_id: str
    requirements_sha256: str
    config_sha256: str
    repo_head: str
    github_repo: str | None = None
    base_branch: str


class BackupManifest(BaseModel):
    version: Literal[1] = _BACKUP_VERSION
    created_at: str
    persistence_backend: Literal["sqlite", "postgres"]
    projects: list[BackupProject]
    files: list[BackupFile]


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _registry_fingerprint(registry: RegistryReader) -> str:
    projects = registry.list_projects()
    payload = []
    for project in sorted(projects, key=lambda item: str(item.get("id"))):
        project_id = str(project["id"])
        runs = registry.runs_for_project(project_id)
        payload.append(
            {
                "project": project,
                "runs": sorted(runs, key=lambda item: str(item.get("id"))),
            }
        )
    raw = json.dumps(payload, ensure_ascii=False, sort_keys=True, default=str).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


def _project_source_fingerprint(project: dict[str, Any], cfg: ProjectConfig) -> str:
    config_path = Path(str(project["config_path"])).expanduser().resolve()
    try:
        head = current_head(cfg.repo_path)
    except GitError as exc:
        raise BackupError(str(exc)) from exc
    payload = {
        "project_id": str(project["id"]),
        "workspace_id": project.get("workspace_id"),
        "state_store_id": project.get("state_store_id"),
        "config_sha256": _sha256(config_path),
        "requirements_sha256": _sha256(cfg.requirements_path),
        "repo_head": head,
    }
    raw = json.dumps(payload, ensure_ascii=False, sort_keys=True).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


def _ensure_quiescent(registry: RegistryReader) -> None:
    blockers: list[str] = []
    for project in registry.list_projects():
        project_id = str(project["id"])
        unfinished = [
            record
            for record in registry.runs_for_project(project_id)
            if not record.get("finished_at")
        ]
        if unfinished:
            statuses = sorted({str(record.get("status") or "unknown") for record in unfinished})
            blockers.append(f"{project_id}: unfinished runs in states {statuses}")
            continue
        config_path = Path(str(project["config_path"])).expanduser().resolve()
        try:
            cfg = load_config(config_path)
            assert_workspace_affinity(project, cfg.repo_path)
            assert_state_store_affinity(project, cfg.state_dir)
        except Exception as exc:
            blockers.append(f"{project_id}: storage/config affinity check failed: {exc}")
            continue
        records = WorkspaceOwnershipStore(cfg.worktree_dir).list_records()
        active = sorted(record.branch for record in records if record.status != "released")
        if active:
            blockers.append(f"{project_id}: active or pending-cleanup worktrees {active}")
            continue
        status = run(["git", "status", "--porcelain"], cwd=cfg.repo_path, timeout=30)
        if status.returncode != 0:
            blockers.append(f"{project_id}: unable to inspect base repository status")
        elif status.stdout.strip():
            blockers.append(f"{project_id}: base repository has uncommitted changes")
    if blockers:
        raise BackupError("Deployment is not quiescent:\n- " + "\n- ".join(blockers))


def _inside(path: Path, parent: Path) -> bool:
    try:
        path.relative_to(parent)
    except ValueError:
        return False
    return True


def _validate_destination(
    destination: Path,
    projects: list[tuple[dict[str, Any], ProjectConfig]],
) -> Path:
    target = destination.expanduser().resolve()
    if target.exists():
        raise BackupError(f"Backup destination already exists: {target}")
    for _, cfg in projects:
        protected = (cfg.repo_path, cfg.state_dir, cfg.worktree_dir)
        if any(_inside(target, root) for root in protected):
            raise BackupError(
                f"Backup destination must be outside repository/state/worktree trees: {target}"
            )
    return target


def _reject_symlinks(root: Path, label: str) -> None:
    if root.is_symlink():
        raise BackupError(f"{label} is a symlink and cannot be backed up safely: {root}")
    if not root.exists() or root.is_file():
        return
    for current, directories, files in os.walk(root, followlinks=False):
        current_path = Path(current)
        for name in [*directories, *files]:
            candidate = current_path / name
            if candidate.is_symlink():
                raise BackupError(f"{label} contains symlink: {candidate}")


def _sqlite_backup(source: Path, destination: Path) -> None:
    source = source.expanduser().resolve()
    if not source.is_file():
        raise BackupError(f"SQLite database not found: {source}")
    destination.parent.mkdir(parents=True, exist_ok=True)
    src = sqlite3.connect(source.as_uri() + "?mode=ro", uri=True)
    dst = sqlite3.connect(destination)
    try:
        src.backup(dst)
    finally:
        dst.close()
        src.close()


def _postgres_backup(database_url: str, destination: Path) -> None:
    executable = shutil.which("pg_dump")
    if executable is None:
        raise BackupError("PostgreSQL backup requires pg_dump on PATH")
    destination.parent.mkdir(parents=True, exist_ok=True)
    try:
        env = libpq_env(database_url, base=os.environ)
    except RuntimeError as exc:
        raise BackupError(str(exc)) from exc
    result = subprocess.run(
        [executable, "--format=custom", "--file", str(destination)],
        env=env,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        timeout=1800,
        check=False,
    )
    if result.returncode != 0:
        detail = result.stdout.strip()[-2000:]
        raise BackupError(f"pg_dump failed with exit code {result.returncode}: {detail}")


def _ignored_state_names(
    source_dir: Path,
    cfg: ProjectConfig,
    control_db_path: Path,
) -> set[Path]:
    excluded = {
        (cfg.state_dir / "langgraph.sqlite").resolve(),
        (cfg.state_dir / "langgraph.sqlite-wal").resolve(),
        (cfg.state_dir / "langgraph.sqlite-shm").resolve(),
        cfg.worktree_dir.resolve(),
    }
    resolved_control = control_db_path.expanduser().resolve()
    if _inside(resolved_control, source_dir):
        excluded.update(
            {
                resolved_control,
                Path(str(resolved_control) + "-wal"),
                Path(str(resolved_control) + "-shm"),
            }
        )
    return excluded


def _copy_state_tree(
    cfg: ProjectConfig,
    destination: Path,
    control_db_path: Path,
) -> None:
    source = cfg.state_dir.resolve()
    _reject_symlinks(source, "state directory")
    destination.mkdir(parents=True, exist_ok=True)
    excluded = _ignored_state_names(source, cfg, control_db_path)
    for current, directories, files in os.walk(source, followlinks=False):
        current_path = Path(current).resolve()
        directories[:] = [
            name
            for name in directories
            if not any(
                current_path / name == excluded_path
                or _inside(current_path / name, excluded_path)
                for excluded_path in excluded
            )
        ]
        relative = current_path.relative_to(source)
        target_dir = destination / relative
        target_dir.mkdir(parents=True, exist_ok=True)
        for name in files:
            source_file = current_path / name
            if source_file in excluded:
                continue
            shutil.copy2(source_file, target_dir / name)


def _git_bundle(repo: Path, destination: Path) -> str:
    try:
        head = current_head(repo)
    except GitError as exc:
        raise BackupError(str(exc)) from exc
    destination.parent.mkdir(parents=True, exist_ok=True)
    result = run(
        ["git", "bundle", "create", str(destination), "--all"],
        cwd=repo,
        timeout=1800,
    )
    if result.returncode != 0:
        raise BackupError(f"git bundle failed: {result.stdout.strip()[-2000:]}")
    return head


def _inventory(root: Path) -> list[BackupFile]:
    files: list[BackupFile] = []
    for path in sorted(root.rglob("*")):
        if path.is_symlink():
            raise BackupError(f"Backup staging contains unexpected symlink: {path}")
        if not path.is_file() or path.name == "manifest.json":
            continue
        files.append(
            BackupFile(
                path=path.relative_to(root).as_posix(),
                size=path.stat().st_size,
                sha256=_sha256(path),
            )
        )
    return files


def create_deployment_backup(
    *,
    registry: RegistryReader,
    persistence_backend: Literal["sqlite", "postgres"],
    control_db_path: Path,
    database_url: str | None,
    destination: Path,
) -> BackupManifest:
    """Create one globally quiescent deployment backup and publish it atomically."""
    if persistence_backend not in {"sqlite", "postgres"}:
        raise BackupError(f"Unsupported persistence backend: {persistence_backend}")
    projects_raw = registry.list_projects()
    if not projects_raw:
        raise BackupError("No registered projects to back up")

    _ensure_quiescent(registry)
    before = _registry_fingerprint(registry)
    projects: list[tuple[dict[str, Any], ProjectConfig]] = []
    source_fingerprints: dict[str, str] = {}
    for project in projects_raw:
        project_id = str(project["id"])
        if _PROJECT_ID_RE.fullmatch(project_id) is None:
            raise BackupError(f"Unsafe project ID in durable registry: {project_id!r}")
        config_path = Path(str(project["config_path"])).expanduser().resolve()
        cfg = load_config(config_path)
        assert_workspace_affinity(project, cfg.repo_path)
        assert_state_store_affinity(project, cfg.state_dir)
        projects.append((project, cfg))
        source_fingerprints[project_id] = _project_source_fingerprint(project, cfg)

    target = _validate_destination(destination, projects)
    staging = target.with_name(f".{target.name}.tmp-{uuid4().hex}")
    staging.mkdir(parents=True, exist_ok=False)
    manifest_projects: list[BackupProject] = []
    try:
        database_dir = staging / "database"
        if persistence_backend == "sqlite":
            _sqlite_backup(control_db_path, database_dir / "control.sqlite")
        else:
            if not database_url:
                raise BackupError("PostgreSQL persistence selected without a database URL")
            _postgres_backup(database_url, database_dir / "postgres.dump")

        for project, cfg in sorted(projects, key=lambda item: str(item[0]["id"])):
            project_id = str(project["id"])
            config_path = Path(str(project["config_path"])).expanduser().resolve()
            _reject_symlinks(config_path, "project configuration")
            _reject_symlinks(cfg.requirements_path, "requirements file")
            project_dir = staging / "projects" / project_id
            project_dir.mkdir(parents=True, exist_ok=False)
            backup_config = project_dir / "converge.yaml"
            backup_requirements = project_dir / "requirements.md"
            shutil.copy2(config_path, backup_config)
            shutil.copy2(cfg.requirements_path, backup_requirements)
            head = _git_bundle(cfg.repo_path, project_dir / "repository.bundle")
            _copy_state_tree(cfg, project_dir / "state", control_db_path)

            if persistence_backend == "sqlite":
                checkpoint = cfg.state_dir / "langgraph.sqlite"
                if checkpoint.is_file():
                    _sqlite_backup(checkpoint, project_dir / "langgraph.sqlite")

            workspace = str(project.get("workspace_id") or "")
            state_store = str(project.get("state_store_id") or "")
            if not workspace or not state_store:
                raise BackupError(f"Project {project_id} has incomplete storage affinity binding")
            manifest_projects.append(
                BackupProject(
                    project_id=project_id,
                    config_source_path=str(config_path),
                    requirements_source_path=str(cfg.requirements_path),
                    repo_source_path=str(cfg.repo_path),
                    state_source_path=str(cfg.state_dir),
                    worktree_source_path=str(cfg.worktree_dir),
                    workspace_id=workspace,
                    state_store_id=state_store,
                    requirements_sha256=_sha256(backup_requirements),
                    config_sha256=_sha256(backup_config),
                    repo_head=head,
                    github_repo=cfg.github_repo,
                    base_branch=cfg.base_branch,
                )
            )

        _ensure_quiescent(registry)
        after = _registry_fingerprint(registry)
        if before != after:
            raise BackupError(
                "Durable registry changed while backup was being created; "
                "refusing inconsistent backup"
            )
        for project, cfg in projects:
            project_id = str(project["id"])
            if _project_source_fingerprint(project, cfg) != source_fingerprints[project_id]:
                raise BackupError(
                    f"Project {project_id} source/config changed during backup; refusing snapshot"
                )

        manifest = BackupManifest(
            created_at=datetime.now(UTC).isoformat(),
            persistence_backend=persistence_backend,
            projects=manifest_projects,
            files=_inventory(staging),
        )
        (staging / "manifest.json").write_text(
            manifest.model_dump_json(indent=2) + "\n",
            encoding="utf-8",
        )
        staging.replace(target)
        return manifest
    except Exception:
        shutil.rmtree(staging, ignore_errors=True)
        raise


def verify_deployment_backup(root: Path) -> BackupManifest:
    """Verify backup file set, sizes and SHA-256 hashes without executing backup content."""
    root = root.expanduser().resolve()
    manifest_path = root / "manifest.json"
    if not manifest_path.is_file() or manifest_path.is_symlink():
        raise BackupError(f"Backup manifest not found or unsafe: {manifest_path}")
    try:
        manifest = BackupManifest.model_validate_json(manifest_path.read_text(encoding="utf-8"))
    except ValueError as exc:
        raise BackupError(f"Invalid backup manifest: {manifest_path}") from exc

    expected = {item.path: item for item in manifest.files}
    actual: set[str] = set()
    for path in sorted(root.rglob("*")):
        if path.is_symlink():
            raise BackupError(f"Backup contains symlink: {path}")
        if not path.is_file() or path == manifest_path:
            continue
        relative = path.relative_to(root).as_posix()
        actual.add(relative)
        item = expected.get(relative)
        if item is None:
            raise BackupError(f"Unexpected backup file: {relative}")
        if path.stat().st_size != item.size:
            raise BackupError(f"Backup size mismatch: {relative}")
        if _sha256(path) != item.sha256:
            raise BackupError(f"Backup SHA-256 mismatch: {relative}")

    missing = sorted(set(expected) - actual)
    if missing:
        raise BackupError(f"Backup is missing files: {missing}")
    return manifest
