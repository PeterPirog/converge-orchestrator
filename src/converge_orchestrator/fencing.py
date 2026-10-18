"""INV-FENCE-001: deterministic controller mutation-fence guard.

Pure, offline primitives for the invariant
INV-FENCE-001 (CONVERGE_ORCHESTRATOR_2_0_SOURCE_OF_TRUTH.md:L177 [4. Global invariants]):
a controller that no longer owns the current run or project mutation fencing epoch SHALL NOT
perform repository, remote, compliance, or other externally visible mutating side effects for
that authority.

``evaluate_mutation_fence`` inspects a plain mapping describing the mutation fencing epochs the
controller claims and the authoritative current epochs, and returns the lost-fence clauses in a
fixed order. The guard fails closed: a fence is lost unless the claimed epoch is present and
exactly matches the authoritative current epoch. Inputs and outputs are plain values so the guard
can later be bound to controller state rows. There is no network, model, provider, subprocess,
git, or filesystem access in this module.
"""

from __future__ import annotations

from collections.abc import Mapping

#: INV-FENCE-001 run clause: the controller's claimed run mutation fencing epoch no longer
#: matches the authoritative current run epoch (it is stale, absent, or mismatched), so the
#: controller no longer owns the current run fence.
RUN_FENCE_LOST = "run_fence_lost"
#: INV-FENCE-001 project clause: the controller's claimed project mutation fencing epoch no
#: longer matches the authoritative current project epoch (it is stale, absent, or mismatched),
#: so the controller no longer owns the current project fence.
PROJECT_FENCE_LOST = "project_fence_lost"

#: Plain-mapping field names describing a mutation-fence state for inspection.
_FIELD_CLAIMED_RUN = "claimed_run_fence_epoch"
_FIELD_CLAIMED_PROJECT = "claimed_project_fence_epoch"
_FIELD_CURRENT_RUN = "current_run_fence_epoch"
_FIELD_CURRENT_PROJECT = "current_project_fence_epoch"


def _holds(claimed: object, current: object) -> bool:
    """Return True only when the claimed epoch positively matches the authoritative one.

    Fails closed: an absent (``None``) claimed epoch cannot prove ownership of the current
    fence, so the fence is treated as lost even if the current epoch is also absent.
    """
    return claimed is not None and claimed == current


def evaluate_mutation_fence(state: Mapping[str, object]) -> tuple[str, ...]:
    """Deterministically flag the mutation fences the controller no longer owns.

    ``state`` maps the plain field names ``claimed_run_fence_epoch``,
    ``claimed_project_fence_epoch``, ``current_run_fence_epoch``, and
    ``current_project_fence_epoch`` to the epochs involved (``None`` when the controller claims
    no epoch for that fence, or when no authoritative current epoch exists).

    Returns the lost-fence clauses in fixed clause order (an empty tuple when the controller
    still owns both fences):

    - ``RUN_FENCE_LOST`` when the claimed run mutation fencing epoch no longer matches the
      authoritative current run epoch (stale, absent, or mismatched);
    - ``PROJECT_FENCE_LOST`` when the claimed project mutation fencing epoch no longer matches
      the authoritative current project epoch (stale, absent, or mismatched).

    A caller receiving a non-empty result SHALL NOT perform repository, remote, compliance, or
    other externally visible mutating side effects for that authority (INV-FENCE-001).
    """
    if not isinstance(state, Mapping):
        raise TypeError("mutation fence state must be a mapping of fence-epoch fields")
    lost: list[str] = []
    if not _holds(state.get(_FIELD_CLAIMED_RUN), state.get(_FIELD_CURRENT_RUN)):
        lost.append(RUN_FENCE_LOST)
    if not _holds(state.get(_FIELD_CLAIMED_PROJECT), state.get(_FIELD_CURRENT_PROJECT)):
        lost.append(PROJECT_FENCE_LOST)
    return tuple(lost)
