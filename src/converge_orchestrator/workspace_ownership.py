from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, Field


class WorkspaceOwnershipError(RuntimeError):
    pass


def _now() -> str:
    return datetime.now(UTC).isoformat()


class WorkspaceOwnership(BaseModel):
    version: Literal[1] = 1
    task_id: str | None = None
    path: str
    branch: str
    status: Literal["active", "cleanup_requested", "released"]
    updated_at: str
    history: list[dict[str, str]] = Field(default_factory=list)


class WorkspaceOwnershipStore:
    """Atomic local ownership ledger for deterministic Converge worktrees."""

    def __init__(self, worktree_root: Path):
        self.worktree_root = worktree_root.expanduser().resolve()
        self.root = self.worktree_root / ".ownership"

    @staticmethod
    def _key(branch: str) -> str:
        return hashlib.sha256(branch.encode("utf-8")).hexdigest()[:24]

    def _path(self, branch: str) -> Path:
        return self.root / f"{self._key(branch)}.json"

    def _validate_target(self, target: Path) -> Path:
        resolved = target.expanduser().resolve()
        if resolved.parent != self.worktree_root:
            raise WorkspaceOwnershipError(
                f"Workspace path is outside the configured worktree root: {resolved}"
            )
        return resolved

    def read(self, branch: str) -> WorkspaceOwnership | None:
        path = self._path(branch)
        if not path.is_file():
            return None
        try:
            record = WorkspaceOwnership.model_validate_json(path.read_text(encoding="utf-8"))
        except ValueError as exc:
            raise WorkspaceOwnershipError(f"Invalid workspace ownership record: {path}") from exc
        self._validate_target(Path(record.path))
        if record.branch != branch:
            raise WorkspaceOwnershipError(
                f"Workspace ownership branch mismatch: expected {branch}, found {record.branch}"
            )
        return record

    def list_records(self) -> list[WorkspaceOwnership]:
        if not self.root.is_dir():
            return []
        records: list[WorkspaceOwnership] = []
        for path in sorted(self.root.glob("*.json")):
            try:
                record = WorkspaceOwnership.model_validate_json(
                    path.read_text(encoding="utf-8")
                )
            except ValueError as exc:
                raise WorkspaceOwnershipError(
                    f"Invalid workspace ownership record: {path}"
                ) from exc
            self._validate_target(Path(record.path))
            if path != self._path(record.branch):
                raise WorkspaceOwnershipError(
                    f"Workspace ownership key does not match branch: {path}"
                )
            records.append(record)
        return records

    def activate(self, *, task_id: str, target: Path, branch: str) -> WorkspaceOwnership:
        target = self._validate_target(target)
        current = self.read(branch)
        if current is not None:
            if Path(current.path).resolve() != target:
                raise WorkspaceOwnershipError(
                    f"Branch {branch} is already owned by another path: {current.path}"
                )
            if current.status == "cleanup_requested":
                raise WorkspaceOwnershipError(
                    f"Workspace {branch} is awaiting cleanup and cannot be reactivated"
                )
        return self._write(
            WorkspaceOwnership(
                task_id=task_id,
                path=str(target),
                branch=branch,
                status="active",
                updated_at=_now(),
                history=self._history(current, "activated"),
            )
        )

    def request_cleanup(
        self,
        *,
        target: Path,
        branch: str,
        reason: str,
    ) -> WorkspaceOwnership:
        target = self._validate_target(target)
        current = self.read(branch)
        if current is not None and Path(current.path).resolve() != target:
            raise WorkspaceOwnershipError(
                f"Cleanup ownership mismatch for {branch}: {current.path} != {target}"
            )
        return self._write(
            WorkspaceOwnership(
                task_id=current.task_id if current else None,
                path=str(target),
                branch=branch,
                status="cleanup_requested",
                updated_at=_now(),
                history=self._history(current, f"cleanup_requested:{reason}"),
            )
        )

    def mark_released(self, *, target: Path, branch: str) -> WorkspaceOwnership:
        target = self._validate_target(target)
        current = self.read(branch)
        if current is None:
            raise WorkspaceOwnershipError(f"No ownership record for released branch {branch}")
        if Path(current.path).resolve() != target:
            raise WorkspaceOwnershipError(
                f"Release ownership mismatch for {branch}: {current.path} != {target}"
            )
        return self._write(
            current.model_copy(
                update={
                    "status": "released",
                    "updated_at": _now(),
                    "history": self._history(current, "released"),
                }
            )
        )

    @staticmethod
    def _history(
        current: WorkspaceOwnership | None,
        event: str,
    ) -> list[dict[str, str]]:
        history = list(current.history) if current else []
        history.append({"at": _now(), "event": event})
        return history[-40:]

    def _write(self, record: WorkspaceOwnership) -> WorkspaceOwnership:
        self.root.mkdir(parents=True, exist_ok=True)
        path = self._path(record.branch)
        temporary = path.with_suffix(".json.tmp")
        temporary.write_text(
            json.dumps(record.model_dump(mode="json"), indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        temporary.replace(path)
        return record
