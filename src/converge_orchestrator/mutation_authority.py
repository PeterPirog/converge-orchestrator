"""ARCH-CTRL-005: fencing-aware mutation authority guard.

Pure, offline predicate for the architecture requirement ARCH-CTRL-005
(CONVERGE_ORCHESTRATOR_2_0_SOURCE_OF_TRUTH.md:L243 [7. Deterministic architecture and agent
roles]): in a deployment where mutation authority can transfer while an earlier controller may
later resume, canonical-repository and remote mutations SHALL be mediated by a fencing-aware
authority that validates the current epoch as part of accepting the mutation. Possession of a
generic long-lived mutation credential by a stale controller SHALL NOT be sufficient to publish.

``can_publish_mutation`` admits the ``"canonical_repository"`` and ``"remote"`` mutation scopes
only when the controller's claimed run and project mutation-fencing epochs both match the
authoritative current epochs. It delegates the epoch comparison to
``converge_orchestrator.fencing.evaluate_mutation_fence``; a mismatch, absent epoch, or unknown
scope returns ``False``, while an invalid (non-mapping) state causes the delegated guard to raise
rather than admit the mutation, remaining fail-closed.

There is no network, model, provider, subprocess, git command, filesystem, or other side-effecting
access in this module.
"""

from __future__ import annotations

from collections.abc import Mapping

from converge_orchestrator.fencing import evaluate_mutation_fence

#: Mutation scopes that ARCH-CTRL-005 mediates through the fencing-aware authority.
_MUTATION_SCOPES = frozenset({"canonical_repository", "remote"})


def can_publish_mutation(scope: str, state: Mapping[str, object]) -> bool:
    """Return ``True`` only when the controller owns the current mutation fence for ``scope``.

    ``state`` is the plain mutation-fence mapping described by
    ``converge_orchestrator.fencing.evaluate_mutation_fence``. The guard admits the mutation only
    when the claimed run and project epochs both exactly match the authoritative current epochs.

    A stale, absent, or mismatched epoch or an unknown ``scope`` causes the guard to fail closed
    and return ``False``. An invalid ``state`` shape is rejected by the delegated fence evaluator
    (which raises ``TypeError``), so the guard never admits a mutation on malformed input.
    """
    if scope not in _MUTATION_SCOPES:
        return False
    lost_fences = evaluate_mutation_fence(state)
    return not lost_fences
