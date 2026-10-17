from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
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


class CandidateBoundEvidence:
    """Candidate-bound evidence with drift invalidation (INV-EVID-001).

    INV-EVID-001 (CONVERGE_ORCHESTRATOR_2_0_SOURCE_OF_TRUTH.md:L166 [4. Global invariants]):
    Candidate-bound evidence SHALL remain valid only for the exact candidate content and policy
    identity it evaluated, and later mutation of that content or policy SHALL invalidate affected
    downstream evidence.

    This primitive binds a unit of evidence to the exact candidate content hash and policy
    identity hash that were present at evaluation time. It remains valid only while both
    identities still equal the values recorded at binding time; once either identity drifts, the
    evidence must be treated as invalid (and so must any downstream evidence that depends on it).

    Identity hashes are SHA-256 digests, consistent with the existing evidence and cache code
    (e.g. ``architecture_policy_sha256`` and the ``candidate_sha256`` fingerprint convention).
    Validation is a pure, deterministic in-memory comparison: no network, model, provider, or
    fetch operations.
    """

    __slots__ = ("candidate_content_sha256", "policy_identity_sha256")

    def __init__(
        self,
        candidate_content_sha256: str,
        policy_identity_sha256: str,
    ) -> None:
        self.candidate_content_sha256 = candidate_content_sha256
        self.policy_identity_sha256 = policy_identity_sha256

    def is_valid(
        self,
        candidate_content_sha256: str,
        policy_identity_sha256: str,
    ) -> bool:
        """Return ``True`` only if both identities exactly match the bound values."""
        return bool(
            candidate_content_sha256 == self.candidate_content_sha256
            and policy_identity_sha256 == self.policy_identity_sha256
        )


class ContentIntegrityBinding:
    """Content-integrity binding for retained convergence evidence (INV-EVID-002).

    INV-EVID-002 (CONVERGE_ORCHESTRATOR_2_0_SOURCE_OF_TRUTH.md:L167 [4. Global invariants]):
    Evidence that contributes to integration, compliance PASS, acceptance, or release convergence
    SHALL be content-integrity-bound so later replacement, truncation, or mutation of the retained
    artifact is detected before that evidence is reused.
    This primitive binds a unit of retained evidence to the exact SHA-256 digest and byte size of
    its content at binding time. ``verify`` stays ``True`` only while the content presented for
    reuse is byte-identical to the content that was bound; any later replacement, truncation, or
    mutation changes the digest (and usually the size), so it is detected before the evidence is
    reused.

    Binding and verification are pure, deterministic in-memory operations: no network, model,
    provider, subprocess, or filesystem side effects. Digests are SHA-256 hex digests, consistent
    with the existing evidence code (e.g. ``hashlib.sha256(...).hexdigest()``).
    """

    __slots__ = ("content_sha256", "content_size")

    def __init__(self, content: bytes) -> None:
        self.content_sha256 = hashlib.sha256(content).hexdigest()
        self.content_size = len(content)

    def verify(self, content: bytes) -> bool:
        """Return ``True`` only if ``content`` is byte-identical to the bound content."""
        return bool(
            len(content) == self.content_size
            and hashlib.sha256(content).hexdigest() == self.content_sha256
        )


class EvidenceClosure:
    """Transitive evidence-dependency closure for terminal convergence (INV-EVID-003).

    INV-EVID-003 (CONVERGE_ORCHESTRATOR_2_0_SOURCE_OF_TRUTH.md:L174 [4. Global invariants]):
    Terminal convergence SHALL have evidence closure for every evidence dependency used by
    mandatory PASS, integration, operational acceptance, and release provenance.

    This primitive computes the transitive closure of the evidence-dependency graph rooted at the
    convergence record ids. Starting from ``root_ids`` it follows every id recorded in
    ``dependencies`` until no new id is reachable, so the full set of transitively reachable
    evidence dependencies is covered. ``missing`` is the exact set of reachable dependencies with
    no resolved evidence (i.e. absent from ``resolved``), and ``closed`` is ``True`` only when
    ``missing`` is empty, so a partial closure is reported as ``closed=False`` alongside the
    stably ordered tuple of unresolved ids.

    Closure computation is pure and deterministic: no network, model, provider, subprocess, or
    filesystem side effects. ``closure`` and ``missing`` are stably sorted tuples of the evidence
    ids, so repeated evaluation of the same inputs is byte-identical.
    """

    __slots__ = ("closure", "missing", "closed")

    def __init__(
        self,
        root_ids: tuple[str, ...],
        dependencies: Mapping[str, tuple[str, ...]],
        resolved: tuple[str, ...],
    ) -> None:
        seen: set[str] = set()
        stack: list[str] = list(root_ids)
        while stack:
            current = stack.pop()
            if current in seen:
                continue
            seen.add(current)
            for dependency in dependencies.get(current, ()):
                if dependency not in seen:
                    stack.append(dependency)
        self.closure = tuple(sorted(seen))
        resolved_ids = set(resolved)
        self.missing = tuple(sorted(eid for eid in seen if eid not in resolved_ids))
        self.closed = not self.missing


class CandidateSnapshotEvidenceReport:
    """Evidence-binding report for one exact candidate snapshot identity (INV-CANDIDATE-001).

    Records, in the canonical required-category order, which categories are bound to the exact
    snapshot identity (``applied``) and which are not (``unbound``). ``complete`` is ``True``
    only when every required category is bound to the exact candidate content identity, i.e. the
    candidate snapshot is fully evidenced.
    """

    __slots__ = ("applied", "unbound", "complete")

    def __init__(
        self,
        applied: tuple[str, ...],
        unbound: tuple[str, ...],
        complete: bool,
    ) -> None:
        self.applied = applied
        self.unbound = unbound
        self.complete = complete


class CandidateSnapshotIdentity:
    """Candidate-snapshot identity binding (INV-CANDIDATE-001).

    INV-CANDIDATE-001 (CONVERGE_ORCHESTRATOR_2_0_SOURCE_OF_TRUTH.md:L168 [4. Global invariants]):
    Scope, quality, verifier, risk, review, and integration-readiness evidence for a candidate
    SHALL apply to one exact publishable candidate snapshot and candidate content identity rather
    than to an incomplete textual patch or mutable ambient working directory.

    This primitive derives one candidate content identity from the complete publishable candidate
    snapshot bytes using a SHA-256 digest. A unit of evidence applies only when its identity
    equals this exact snapshot identity; evidence tied to a partial textual patch or to a mutable
    ambient working directory carries a different identity and therefore does not apply, and the
    affected categories are reported unbound by :meth:`evaluate`.

    Identity derivation and binding evaluation are pure, deterministic in-memory operations: no
    network, model, provider, subprocess, or ambient-directory reads. Digests are SHA-256 hex
    digests, consistent with the existing evidence code (e.g. ``hashlib.sha256(...).hexdigest()``).
    """

    #: Evidence categories that must each be bound to the exact snapshot identity.
    REQUIRED_EVIDENCE_CATEGORIES = (
        "scope",
        "quality",
        "verifier",
        "risk",
        "review",
        "integration_readiness",
    )

    __slots__ = ("identity_sha256", "snapshot_size")

    def __init__(self, snapshot_bytes: bytes) -> None:
        self.identity_sha256 = hashlib.sha256(snapshot_bytes).hexdigest()
        self.snapshot_size = len(snapshot_bytes)

    def applies_to(self, evidence_identity_sha256: str) -> bool:
        """Return ``True`` only if the evidence identity is this exact snapshot identity."""
        return bool(evidence_identity_sha256 == self.identity_sha256)

    def evaluate(self, evidence_identities: Mapping[str, str]) -> CandidateSnapshotEvidenceReport:
        """Report which required categories are bound to this exact snapshot identity.

        ``evidence_identities`` maps an evidence category (e.g. ``"scope"``) to the SHA-256
        identity of the candidate content that evidence was evaluated against. A category counts
        as applied only when that identity exactly equals ``identity_sha256``; otherwise it is
        reported unbound. Categories outside :data:`REQUIRED_EVIDENCE_CATEGORIES` are ignored.
        """
        applied: list[str] = []
        unbound: list[str] = []
        for category in self.REQUIRED_EVIDENCE_CATEGORIES:
            if evidence_identities.get(category) == self.identity_sha256:
                applied.append(category)
            else:
                unbound.append(category)
        return CandidateSnapshotEvidenceReport(
            applied=tuple(applied),
            unbound=tuple(unbound),
            complete=not unbound,
        )


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
