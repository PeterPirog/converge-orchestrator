"""Lock INV-PROJECT-001: at most one active mutation-capable candidate per integration line.

INV-PROJECT-001 (CONVERGE_ORCHESTRATOR_2_0_SOURCE_OF_TRUTH.md:L164 [4. Global invariants]):
At most one mutation-capable candidate SHALL be active for one project integration line at a time.

This deterministic test pins the cross-branch exclusivity gap in
``WorkspaceOwnershipStore.activate``: while candidate A holds an active workspace on a project
integration line (worktree-root namespace), a second, different candidate B must be refused
activation of its own workspace (different branch, different path) on the same line. The
existing per-branch guard only fires when the SAME branch is targeted, so today candidate B
silently becomes a second active candidate on the same integration line.

The failure marker ``PROJECT001_SECOND_ACTIVE_CANDIDATE_PERMITTED`` is emitted only when the
second activation is (incorrectly) permitted, so it is a literal, behavior-backed RED signal.
No network, models, or subprocess calls: the ledger is a local filesystem store.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from converge_orchestrator.workspace_ownership import (
    WorkspaceOwnershipError,
    WorkspaceOwnershipStore,
)


def test_activate_refuses_second_active_candidate_on_same_line(tmp_path: Path) -> None:
    """A different candidate cannot become a second active candidate on the same line."""
    store = WorkspaceOwnershipStore(tmp_path)

    # Candidate A legitimately activates its workspace on this project integration line.
    store.activate(
        task_id="CANDIDATE-A",
        target=tmp_path / "candidate-a",
        branch="converge/feature-a",
    )

    # Candidate B attempts to activate its own workspace on the SAME integration line.
    try:
        store.activate(
            task_id="CANDIDATE-B",
            target=tmp_path / "candidate-b",
            branch="converge/feature-b",
        )
    except WorkspaceOwnershipError:
        pass  # the required single-active-candidate guard: second activation refused
    else:
        pytest.fail(
            "PROJECT001_SECOND_ACTIVE_CANDIDATE_PERMITTED: "
            "a second, different candidate activated an active workspace on the same "
            "project integration line"
        )

    # Candidate A's active ownership on the line is preserved; no takeover ever happened.
    record = store.read("converge/feature-a")
    assert record is not None
    assert record.task_id == "CANDIDATE-A"
    assert record.status == "active"
    assert Path(record.path) == tmp_path / "candidate-a"
