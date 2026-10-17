"""Lock INV-MODE-002: writer authority is explicit and exclusive per candidate.

INV-MODE-002 (CONVERGE_ORCHESTRATOR_2_0_SOURCE_OF_TRUTH.md:L158 [4. Global invariants]):
Writer authority SHALL be explicit and exclusive per candidate, and changing the authoring
mechanism between candidates SHALL NOT create a weaker verification or integration path.

This deterministic test pins the exclusivity gap in ``WorkspaceOwnershipStore.activate``: a
candidate whose workspace is already actively owned by a DIFFERENT candidate task_id must be
refused a takeover at the same path, so it cannot inherit the existing candidate's writer
authority and bypass a fresh authority/verification path.

The failure marker ``MODE002_CANDIDATE_AUTHORITY_NOT_EXCLUSIVE`` is emitted only when the
takeover is (incorrectly) permitted, so it is a literal, behavior-backed RED signal.
No network, models, or subprocess calls: the ledger is a local filesystem store.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from converge_orchestrator.workspace_ownership import (
    WorkspaceOwnershipError,
    WorkspaceOwnershipStore,
)


def test_activate_refuses_different_candidate_takeover_of_active_workspace(tmp_path: Path) -> None:
    """A different candidate cannot inherit writer authority for an actively owned workspace."""
    store = WorkspaceOwnershipStore(tmp_path)
    target = tmp_path / "candidate-a"
    branch = "converge/feature"

    # Candidate A legitimately acquires exclusive writer authority for this workspace.
    store.activate(task_id="CANDIDATE-A", target=target, branch=branch)

    # Candidate B attempts to take over the SAME actively owned workspace.
    try:
        store.activate(task_id="CANDIDATE-B", target=target, branch=branch)
    except WorkspaceOwnershipError:
        pass  # the required exclusive-authority guard: takeover refused
    else:
        pytest.fail(
            "MODE002_CANDIDATE_AUTHORITY_NOT_EXCLUSIVE: "
            "a different candidate inherited writer authority for an actively owned workspace"
        )

    # The original writer's authority is preserved; the takeover never happened.
    record = store.read(branch)
    assert record is not None
    assert record.task_id == "CANDIDATE-A"
    assert record.status == "active"
    assert Path(record.path) == target
