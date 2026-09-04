from __future__ import annotations

import json
import os
import shutil
import sqlite3
import subprocess
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field

from .backup import BackupManifest, BackupProject, verify_deployment_backup
from .restore import RestorePlan, plan_deployment_restore

_APPLY_JOURNAL_VERSION = 1


class RestoreApplyError(RuntimeError):
    pass


class RestoreApplyResult(BaseModel):
    status: str = "restored"
    persistence_backend: str
    projects: list[str]
    resumed: bool = False


class _ApplyJournal(BaseModel):
    version: int = _APPLY_JOURNAL_VERSION
    confirmation_token: str
    manifest_sha256: str
    plan: dict[str, Any]
    published: list[str] = Field(default_factory=list)


def _sha256(path: Path) -> str:
    import hashlib

    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _link_like(path: Path) -> bool:
    if path.is_symlink():
        return True
    is_junction = getattr(path, "is_junction", None)
    return bool(is_junction and is_junction())


def _inside(path: Path, parent: Path) -> bool:
    try:
        path.relative_to(parent)
    except ValueError:
        return False
    return True


def _stage_path(target: Path, token: str) -> Path:
    return target.parent / f".{target.name}.converge-restore-{token[:16]}.stage"


def _journal_path(root: Path, token: str) -> Path:
    return root.parent / f".{root.name}.converge-restore-{token[:16]}.json"


def _atomic_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    with temporary.open("w", encoding="utf-8", newline="\n") as handle:
        json.dump(payload, handle, ensure_ascii=False, indent=2, sort_keys=True)
        handle.write("\n")
        handle.flush()
        os.fsync(handle.fileno())
    temporary.replace(path)


def _create_journal(path: Path, journal: _ApplyJournal) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    try:
        fd = os.open(path, flags, 0o600)
    except FileExistsError as exc:
        raise RestoreApplyError(f"restore journal already exists: {path}") from exc
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(journal.model_dump_json(indent=2) + "\n")
            handle.flush()
            os.fsync(handle.fileno())
    except Exception:
        path.unlink(missing_ok=True)
        raise


def _load_journal(path: Path) -> _ApplyJournal:
    if _link_like(path) or not path.is_file():
        raise RestoreApplyError(f"restore journal is missing or unsafe: {path}")
    try:
        return _ApplyJournal.model_validate_json(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise RestoreApplyError(f"restore journal is unreadable or invalid: {path}") from exc


def _write_journal(path: Path, journal: _ApplyJournal) -> None:
    _atomic_json(path, journal.model_dump(mode="json"))


def _sqlite_quick_check(path: Path) -> None:
    try:
        db = sqlite3.connect(path.as_uri() + "?mode=ro", uri=True)
        try:
            row = db.execute("PRAGMA quick_check").fetchone()
        finally:
            db.close()
    except sqlite3.DatabaseError as exc:
        raise RestoreApplyError(f"restored SQLite artifact is unreadable: {path.name}") from exc
    if row is None or str(row[0]).lower() != "ok":
        raise RestoreApplyError(f"restored SQLite artifact failed quick_check: {path.name}")


def _copy_file(source: Path, stage: Path, expected_sha256: str) -> None:
    if stage.exists() or _link_like(stage):
        stage.unlink(missing_ok=True)
    stage.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(source, stage)
    if _sha256(stage) != expected_sha256:
        stage.unlink(missing_ok=True)
        raise RestoreApplyError(f"staged file hash mismatch: {source.name}")


def _ensure_file(
    *,
    key: str,
    source: Path,
    target: Path,
    expected_sha256: str,
    journal: _ApplyJournal,
    journal_path: Path,
) -> None:
    if target.exists() or _link_like(target):
        if not target.is_file() or _link_like(target) or _sha256(target) != expected_sha256:
            raise RestoreApplyError(f"restore target is occupied or changed: {target}")
        if key not in journal.published:
            journal.published.append(key)
            _write_journal(journal_path, journal)
        return

    stage = _stage_path(target, journal.confirmation_token)
    _copy_file(source, stage, expected_sha256)
    target.parent.mkdir(parents=True, exist_ok=True)
    os.replace(stage, target)
    if _sha256(target) != expected_sha256:
        raise RestoreApplyError(f"published file validation failed: {target}")
    if key not in journal.published:
        journal.published.append(key)
        _write_journal(journal_path, journal)


def _run_git(args: list[str], cwd: Path | None = None) -> str:
    try:
        result = subprocess.run(
            ["git", *args],
            cwd=cwd,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            timeout=300,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise RestoreApplyError("git restore operation could not be executed") from exc
    if result.returncode != 0:
        raise RestoreApplyError(
            f"git restore operation failed with exit code {result.returncode}: "
            f"{result.stdout.strip()[-1000:]}"
        )
    return result.stdout.strip()


def _validate_repo(target: Path, project: BackupProject) -> None:
    if _link_like(target) or not target.is_dir():
        raise RestoreApplyError(f"restored repository target is missing or unsafe: {target}")
    head = _run_git(["rev-parse", "HEAD"], cwd=target)
    if head != project.repo_head:
        raise RestoreApplyError(f"restored repository HEAD mismatch for {project.project_id}")
    marker = target / ".git" / "converge-workspace-id"
    if _link_like(marker) or not marker.is_file():
        raise RestoreApplyError(f"restored workspace marker is missing for {project.project_id}")
    if marker.read_text(encoding="utf-8").strip() != project.workspace_id:
        raise RestoreApplyError(f"restored workspace identity mismatch for {project.project_id}")


def _ensure_repo(
    root: Path,
    project: BackupProject,
    target: Path,
    journal: _ApplyJournal,
    journal_path: Path,
) -> None:
    key = f"{project.project_id}:repository"
    if target.exists() or _link_like(target):
        _validate_repo(target, project)
        if key not in journal.published:
            journal.published.append(key)
            _write_journal(journal_path, journal)
        return

    bundle = root / "projects" / project.project_id / "repository.bundle"
    stage = _stage_path(target, journal.confirmation_token)
    if stage.exists() or _link_like(stage):
        if stage.is_dir() and not _link_like(stage):
            shutil.rmtree(stage)
        else:
            stage.unlink(missing_ok=True)
    stage.parent.mkdir(parents=True, exist_ok=True)
    _run_git(["clone", "--no-hardlinks", str(bundle), str(stage)])
    _run_git(["checkout", "-B", project.base_branch, project.repo_head], cwd=stage)
    marker = stage / ".git" / "converge-workspace-id"
    marker.write_text(project.workspace_id + "\n", encoding="utf-8")
    _validate_repo(stage, project)
    os.replace(stage, target)
    _validate_repo(target, project)
    journal.published.append(key)
    _write_journal(journal_path, journal)


def _expected_state_files(
    manifest: BackupManifest,
    project: BackupProject,
) -> dict[str, str]:
    prefix = f"projects/{project.project_id}/state/"
    expected = {
        item.path[len(prefix) :]: item.sha256
        for item in manifest.files
        if item.path.startswith(prefix)
    }
    checkpoint = f"projects/{project.project_id}/langgraph.sqlite"
    for item in manifest.files:
        if item.path == checkpoint:
            expected["langgraph.sqlite"] = item.sha256
    return expected


def _validate_state(
    target: Path,
    project: BackupProject,
    expected: dict[str, str],
    *,
    ignored: set[Path],
) -> None:
    if _link_like(target) or not target.is_dir():
        raise RestoreApplyError(f"restored state target is missing or unsafe: {target}")
    marker = target / ".converge-state-id"
    if _link_like(marker) or not marker.is_file():
        raise RestoreApplyError(f"restored state marker is missing for {project.project_id}")
    if marker.read_text(encoding="utf-8").strip() != project.state_store_id:
        raise RestoreApplyError(f"restored state identity mismatch for {project.project_id}")

    actual: set[str] = set()
    for path in target.rglob("*"):
        if any(path == item or _inside(path, item) for item in ignored):
            continue
        if _link_like(path):
            raise RestoreApplyError(f"restored state contains unsafe link: {path}")
        if not path.is_file():
            continue
        relative = path.relative_to(target).as_posix()
        actual.add(relative)
        digest = expected.get(relative)
        if digest is None or _sha256(path) != digest:
            raise RestoreApplyError(f"restored state content mismatch: {relative}")
    if actual != set(expected):
        raise RestoreApplyError(f"restored state file set mismatch for {project.project_id}")

    checkpoint = target / "langgraph.sqlite"
    if checkpoint.is_file():
        _sqlite_quick_check(checkpoint)


def _ensure_state(
    root: Path,
    manifest: BackupManifest,
    project: BackupProject,
    target: Path,
    worktree_target: Path,
    database_target: Path,
    journal: _ApplyJournal,
    journal_path: Path,
) -> None:
    key = f"{project.project_id}:state"
    expected = _expected_state_files(manifest, project)
    ignored: set[Path] = set()
    if _inside(worktree_target, target):
        ignored.add(worktree_target)
    if _inside(database_target, target):
        ignored.add(database_target)

    if target.exists() or _link_like(target):
        _validate_state(target, project, expected, ignored=ignored)
        if key not in journal.published:
            journal.published.append(key)
            _write_journal(journal_path, journal)
        return

    source = root / "projects" / project.project_id / "state"
    stage = _stage_path(target, journal.confirmation_token)
    if stage.exists() or _link_like(stage):
        if stage.is_dir() and not _link_like(stage):
            shutil.rmtree(stage)
        else:
            stage.unlink(missing_ok=True)
    stage.parent.mkdir(parents=True, exist_ok=True)
    shutil.copytree(source, stage, symlinks=False)
    checkpoint = root / "projects" / project.project_id / "langgraph.sqlite"
    if checkpoint.is_file():
        shutil.copy2(checkpoint, stage / "langgraph.sqlite")
    if _inside(worktree_target, target):
        relative_worktree = worktree_target.relative_to(target)
        (stage / relative_worktree).mkdir(parents=True, exist_ok=True)
    stage_ignored = {
        stage / item.relative_to(target)
        for item in ignored
        if _inside(item, target)
    }
    _validate_state(stage, project, expected, ignored=stage_ignored)
    os.replace(stage, target)
    _validate_state(target, project, expected, ignored=ignored)
    journal.published.append(key)
    _write_journal(journal_path, journal)


def _ensure_empty_worktree(
    *,
    project: BackupProject,
    target: Path,
    state_target: Path,
    journal: _ApplyJournal,
    journal_path: Path,
) -> None:
    if _inside(target, state_target):
        return
    key = f"{project.project_id}:worktree"
    if target.exists() or _link_like(target):
        if _link_like(target) or not target.is_dir() or any(target.iterdir()):
            raise RestoreApplyError(f"restored worktree target is not empty: {target}")
        if key not in journal.published:
            journal.published.append(key)
            _write_journal(journal_path, journal)
        return
    stage = _stage_path(target, journal.confirmation_token)
    if stage.exists() or _link_like(stage):
        if stage.is_dir() and not _link_like(stage):
            shutil.rmtree(stage)
        else:
            stage.unlink(missing_ok=True)
    stage.parent.mkdir(parents=True, exist_ok=True)
    stage.mkdir()
    os.replace(stage, target)
    journal.published.append(key)
    _write_journal(journal_path, journal)


def _manifest_project(manifest: BackupManifest, project_id: str) -> BackupProject:
    matches = [project for project in manifest.projects if project.project_id == project_id]
    if len(matches) != 1:
        raise RestoreApplyError(f"backup project identity is ambiguous: {project_id}")
    return matches[0]


def _resume_plan(journal: _ApplyJournal) -> RestorePlan:
    try:
        return RestorePlan.model_validate(journal.plan)
    except ValueError as exc:
        raise RestoreApplyError("restore journal contains an invalid plan") from exc


def apply_sqlite_restore(
    backup_root: Path,
    *,
    confirmation_token: str,
    control_db_path: Path,
    database_url: str | None,
) -> RestoreApplyResult:
    """Apply or resume an operator-approved SQLite restore with database publication last."""
    root = backup_root.expanduser().resolve()
    if len(confirmation_token) != 64 or any(
        char not in "0123456789abcdef" for char in confirmation_token.lower()
    ):
        raise RestoreApplyError("confirmation token must be a 64-character SHA-256 value")

    manifest = verify_deployment_backup(root)
    if manifest.persistence_backend != "sqlite":
        raise RestoreApplyError(
            "PostgreSQL restore apply is not implemented yet; use restore-plan only"
        )
    if database_url:
        raise RestoreApplyError(
            "SQLite restore apply refuses a host configured with CONVERGE_DATABASE_URL"
        )

    manifest_hash = _sha256(root / "manifest.json")
    journal_path = _journal_path(root, confirmation_token)
    resumed = journal_path.exists() or _link_like(journal_path)
    if resumed:
        journal = _load_journal(journal_path)
        if journal.confirmation_token != confirmation_token:
            raise RestoreApplyError("restore journal confirmation token mismatch")
        if journal.manifest_sha256 != manifest_hash:
            raise RestoreApplyError("backup manifest changed after restore apply started")
        plan = _resume_plan(journal)
        if plan.persistence_backend != "sqlite":
            raise RestoreApplyError("restore journal backend mismatch")
        if plan.database_target != str(control_db_path.expanduser().resolve()):
            raise RestoreApplyError("SQLite restore target changed after restore apply started")
    else:
        plan = plan_deployment_restore(
            root,
            control_db_path=control_db_path,
            database_url=database_url,
        )
        if not plan.ready:
            raise RestoreApplyError("restore preflight is blocked; run restore-plan for evidence")
        if plan.confirmation_token != confirmation_token:
            raise RestoreApplyError(
                "confirmation token does not match the current restore preflight"
            )
        journal = _ApplyJournal(
            confirmation_token=confirmation_token,
            manifest_sha256=manifest_hash,
            plan=plan.model_dump(mode="json"),
        )
        _create_journal(journal_path, journal)

    database_target = Path(plan.database_target)
    for project_plan in plan.projects:
        project = _manifest_project(manifest, project_plan.project_id)
        config_target = Path(project_plan.config_target)
        requirements_target = Path(project_plan.requirements_target)
        repo_target = Path(project_plan.repo_target)
        state_target = Path(project_plan.state_target)
        worktree_target = Path(project_plan.worktree_target)
        project_root = root / "projects" / project.project_id

        _ensure_file(
            key=f"{project.project_id}:requirements",
            source=project_root / "requirements.md",
            target=requirements_target,
            expected_sha256=project.requirements_sha256,
            journal=journal,
            journal_path=journal_path,
        )
        _ensure_file(
            key=f"{project.project_id}:config",
            source=project_root / "converge.yaml",
            target=config_target,
            expected_sha256=project.config_sha256,
            journal=journal,
            journal_path=journal_path,
        )
        _ensure_repo(root, project, repo_target, journal, journal_path)
        _ensure_state(
            root,
            manifest,
            project,
            state_target,
            worktree_target,
            database_target,
            journal,
            journal_path,
        )
        _ensure_empty_worktree(
            project=project,
            target=worktree_target,
            state_target=state_target,
            journal=journal,
            journal_path=journal_path,
        )

    database_source = root / "database" / "control.sqlite"
    database_sha = _sha256(database_source)
    _ensure_file(
        key="database",
        source=database_source,
        target=database_target,
        expected_sha256=database_sha,
        journal=journal,
        journal_path=journal_path,
    )
    _sqlite_quick_check(database_target)

    journal_path.unlink(missing_ok=True)
    return RestoreApplyResult(
        persistence_backend="sqlite",
        projects=[project.project_id for project in manifest.projects],
        resumed=resumed,
    )
