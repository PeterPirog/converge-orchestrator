from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any


class EvidenceStore:
    def __init__(self, root: Path):
        self.root = root
        self.root.mkdir(parents=True, exist_ok=True)

    def task_dir(self, run_id: str, task_id: str) -> Path:
        target = self.root / run_id / task_id
        target.mkdir(parents=True, exist_ok=True)
        return target

    def write_json(self, run_id: str, task_id: str, name: str, payload: Any) -> Path:
        path = self.task_dir(run_id, task_id) / name
        self._atomic_write(path, json.dumps(payload, ensure_ascii=False, indent=2) + "\n")
        return path

    def write_text(self, run_id: str, task_id: str, name: str, text: str) -> Path:
        path = self.task_dir(run_id, task_id) / name
        self._atomic_write(path, text)
        return path

    def read_json(self, run_id: str, task_id: str, name: str) -> Any | None:
        """Read one JSON artifact without scanning the rest of the task evidence bundle."""
        path = self.root / run_id / task_id / name
        if not path.is_file():
            return None
        return json.loads(path.read_text(encoding="utf-8"))

    def append_event(self, run_id: str, event: str, payload: dict[str, Any]) -> None:
        run_dir = self.root / run_id
        run_dir.mkdir(parents=True, exist_ok=True)
        record = {
            "timestamp": datetime.now(UTC).isoformat(),
            "event": event,
            "payload": payload,
        }
        with (run_dir / "events.jsonl").open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(record, ensure_ascii=False) + "\n")

    def read_task_bundle(self, run_id: str, task_id: str) -> dict[str, Any]:
        target = self.root / run_id / task_id
        if not target.is_dir():
            raise FileNotFoundError(target)
        artifacts: dict[str, Any] = {}
        for path in sorted(target.iterdir()):
            if not path.is_file():
                continue
            text = path.read_text(encoding="utf-8", errors="replace")
            if path.suffix == ".json":
                try:
                    artifacts[path.name] = json.loads(text)
                    continue
                except json.JSONDecodeError:
                    pass
            artifacts[path.name] = text
        return {"run_id": run_id, "task_id": task_id, "artifacts": artifacts}

    def find_task_bundles(self, task_id: str) -> list[dict[str, Any]]:
        matches: list[dict[str, Any]] = []
        if not self.root.exists():
            return matches
        for run_dir in sorted(self.root.iterdir()):
            if not run_dir.is_dir():
                continue
            target = run_dir / task_id
            if target.is_dir():
                matches.append(self.read_task_bundle(run_dir.name, task_id))
        return matches

    def frozen_red_dir(self, run_id: str, task_id: str) -> Path:
        """Directory for durable frozen RED test file snapshots."""
        target = self.root / run_id / "frozen-red" / task_id
        target.mkdir(parents=True, exist_ok=True)
        return target

    def snapshot_frozen_red(
        self,
        run_id: str,
        task_id: str,
        worktree: Path,
        red_test_hashes: dict[str, str],
    ) -> dict[str, dict[str, Any]]:
        """
        Create exact-byte snapshots of verified frozen RED test files.

        Returns metadata for each snapshotted file including path, sha256, and size.
        """
        snapshots: dict[str, dict[str, Any]] = {}
        frozen_dir = self.frozen_red_dir(run_id, task_id)

        for rel_path, expected_sha256 in red_test_hashes.items():
            # Validate and confine path
            try:
                source = _resolve_confined_path(worktree, rel_path)
            except ValueError:
                # Skip invalid paths - they cannot be snapshotted
                continue
            if not source.is_file():
                continue
            content = source.read_bytes()
            actual_sha256 = hashlib.sha256(content).hexdigest()
            if actual_sha256 != expected_sha256:
                raise ValueError(
                    f"Frozen RED file {rel_path} hash mismatch at snapshot time: "
                    f"expected {expected_sha256}, got {actual_sha256}"
                )
            # Store exact bytes using opaque blob name (sha256) to avoid path traversal in storage
            blob_name = actual_sha256
            target = frozen_dir / blob_name
            target.parent.mkdir(parents=True, exist_ok=True)
            self._atomic_write_bytes(target, content)
            snapshots[rel_path] = {
                "sha256": expected_sha256,
                "size": len(content),
                "blob": blob_name,
            }
        # Write manifest
        manifest = {
            "task_id": task_id,
            "snapshot_time": datetime.now(UTC).isoformat(),
            "files": snapshots,
        }
        self.write_json(run_id, f"frozen-red/{task_id}", "manifest.json", manifest)
        return snapshots

    def verify_frozen_red(
        self,
        run_id: str,
        task_id: str,
        worktree: Path,
    ) -> tuple[bool, dict[str, dict[str, Any]]]:
        """
        Verify current worktree frozen RED files against durable snapshot.

        Returns (all_unchanged, details_per_file).
        """
        manifest_path = self.root / run_id / "frozen-red" / task_id / "manifest.json"
        if not manifest_path.is_file():
            return True, {}
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        expected_files = manifest.get("files", {})

        details: dict[str, dict[str, Any]] = {}
        all_unchanged = True

        for rel_path, expected in expected_files.items():
            expected_sha256 = expected["sha256"]
            # Validate and confine path
            try:
                source = _resolve_confined_path(worktree, rel_path)
            except ValueError:
                details[rel_path] = {
                    "status": "invalid_path",
                    "expected_sha256": expected_sha256,
                    "observed_sha256": None,
                }
                all_unchanged = False
                continue
            if not source.is_file():
                details[rel_path] = {
                    "status": "deleted",
                    "expected_sha256": expected_sha256,
                    "observed_sha256": None,
                }
                all_unchanged = False
                continue
            content = source.read_bytes()
            actual_sha256 = hashlib.sha256(content).hexdigest()
            if actual_sha256 != expected_sha256:
                details[rel_path] = {
                    "status": "modified",
                    "expected_sha256": expected_sha256,
                    "observed_sha256": actual_sha256,
                    "expected_size": expected["size"],
                    "observed_size": len(content),
                }
                all_unchanged = False
            else:
                details[rel_path] = {
                    "status": "unchanged",
                    "sha256": expected_sha256,
                }
        return all_unchanged, details

    def restore_frozen_red(
        self,
        run_id: str,
        task_id: str,
        worktree: Path,
    ) -> dict[str, str]:
        """
        Restore exact authoritative RED bytes from durable snapshot to worktree.

        Returns mapping of restored paths.
        """
        manifest_path = self.root / run_id / "frozen-red" / task_id / "manifest.json"
        if not manifest_path.is_file():
            return {}
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        expected_files = manifest.get("files", {})

        restored: dict[str, str] = {}
        frozen_dir = self.frozen_red_dir(run_id, task_id)

        for rel_path, expected in expected_files.items():
            # Validate and confine path
            try:
                target = _resolve_confined_path(worktree, rel_path)
            except ValueError:
                # Skip invalid paths - they cannot be restored
                continue
            expected_sha256 = expected.get("sha256")
            if not expected_sha256:
                continue
            blob_name = expected.get("blob", expected_sha256)
            if not blob_name:
                continue
            source = frozen_dir / blob_name
            if not source.is_file():
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            content = source.read_bytes()
            # Verify restored content matches expected hash
            actual_sha256 = hashlib.sha256(content).hexdigest()
            if actual_sha256 != expected_sha256:
                raise ValueError(
                    f"Restored content hash mismatch for {rel_path}: "
                    f"expected {expected_sha256}, got {actual_sha256}"
                )
            target.write_bytes(content)
            restored[rel_path] = str(target)
        return restored

    @staticmethod
    def _atomic_write(path: Path, content: str) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_suffix(path.suffix + ".tmp")
        temporary.write_text(content, encoding="utf-8")
        temporary.replace(path)

    @staticmethod
    def _atomic_write_bytes(path: Path, content: bytes) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_suffix(path.suffix + ".tmp")
        temporary.write_bytes(content)
        temporary.replace(path)


def _resolve_confined_path(worktree: Path, rel_path: str) -> Path:
    """
    Resolve a repository-relative path confined to the worktree.

    Security guarantees:
    - Rejects empty/invalid paths
    - Rejects POSIX absolute paths
    - Rejects Windows absolute/drive-qualified/UNC paths
    - Normalizes both '/' and '\\' separators
    - Rejects any '..' traversal component
    - Resolves worktree root canonically (follows symlinks)
    - Resolves candidate path and proves it remains under canonical worktree root
    - Prevents symlink/junction escape outside canonical worktree
    """
    if not rel_path or not rel_path.strip():
        raise ValueError("Empty path not allowed")

    # Check for UNC paths on original input (before normalization)
    if rel_path.startswith("\\\\") or rel_path.startswith("//"):
        raise ValueError(f"UNC path not allowed: {rel_path}")

    # Normalize separators early
    normalized = rel_path.replace("\\", "/")

    # Reject POSIX absolute
    if normalized.startswith("/"):
        raise ValueError(f"Absolute POSIX path not allowed: {rel_path}")

    # Reject Windows drive-qualified (C:, D:, etc.) - both C:/ and C:\ forms
    if len(normalized) >= 2 and normalized[1] == ":" and normalized[0].isalpha():
        raise ValueError(f"Windows drive-qualified path not allowed: {rel_path}")

    # Reject any '..' component (after normalization)
    parts = normalized.split("/")
    for part in parts:
        if part == "..":
            raise ValueError(f"Path traversal not allowed: {rel_path}")

    # Resolve worktree canonically (follows symlinks to real path)
    canonical_worktree = worktree.resolve()

    # Resolve the target path relative to worktree
    target = (worktree / normalized).resolve()

    # Prove target remains under canonical worktree
    try:
        target.relative_to(canonical_worktree)
    except ValueError as exc:
        raise ValueError(f"Path escapes worktree: {rel_path}") from exc

    # Additional check: ensure the resolved path's parent chain doesn't escape
    # via symlinks that were resolved during .resolve()
    # This is already covered by relative_to check above since both are resolved

    return target
