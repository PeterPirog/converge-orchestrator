"""ARCH-CTRL-006: deterministic domain-keyed repository mutation lease guard.

Pure, offline primitive for the architecture requirement ARCH-CTRL-006
(CONVERGE_ORCHESTRATOR_2_0_SOURCE_OF_TRUTH.md:L244 [7. Deterministic architecture and agent
roles]): repository mutation lease and fencing authority SHALL be keyed by repository mutation
domain identity rather than project ID alone and SHALL be authoritative across every controller
permitted to mutate that domain.

``evaluate_repository_mutation_lease`` inspects a plain mapping describing the controller's
claimed repository mutation domain identity (INV-MUTATION-001 identity) and lease epoch together
with the authoritative domain identity and current lease epoch. It returns the lost-lease clauses
in a fixed order. The guard fails closed: the lease is lost unless the claimed domain identity
and lease epoch are both present and exactly match the authoritative values. Inputs and outputs
are plain values so the guard can later be bound to controller state rows. There is no network,
model, provider, subprocess, git, or filesystem access in this module.
"""

from __future__ import annotations

from collections.abc import Mapping

#: ARCH-CTRL-006 domain-identity clause: the controller's claimed mutation-domain identity is
#: absent or differs from the authoritative repository mutation domain identity, so the controller
#: no longer owns the lease for that domain.
DOMAIN_IDENTITY_LOST = "domain_identity_lost"
#: ARCH-CTRL-006 lease-epoch clause: the controller's claimed lease epoch is absent, stale, or
#: mismatched against the authoritative current lease epoch, so the controller no longer owns the
#: current lease for that domain.
LEASE_EPOCH_LOST = "lease_epoch_lost"

#: Plain-mapping field names describing a repository mutation lease state for inspection.
_FIELD_CLAIMED_DOMAIN = "claimed_mutation_domain_id"
_FIELD_AUTHORITATIVE_DOMAIN = "authoritative_mutation_domain_id"
_FIELD_CLAIMED_EPOCH = "claimed_lease_epoch"
_FIELD_AUTHORITATIVE_EPOCH = "authoritative_lease_epoch"


def _domain_holds(claimed: object, authoritative: object) -> bool:
    """Return True only when the claimed domain identity positively matches the authoritative one.

    Fails closed: an absent (``None``) claimed domain identity cannot prove ownership of the
    domain lease, so the identity is treated as lost even if the authoritative domain is also
    absent.
    """
    return claimed is not None and claimed == authoritative


def _epoch_holds(claimed: object, authoritative: object) -> bool:
    """Return True only when the claimed lease epoch positively matches the authoritative one.

    Fails closed: an absent (``None``) claimed epoch cannot prove ownership of the current
    lease, so the epoch is treated as lost even if the authoritative epoch is also absent.
    """
    return claimed is not None and claimed == authoritative


def evaluate_repository_mutation_lease(state: Mapping[str, object]) -> tuple[str, ...]:
    """Deterministically flag the repository mutation lease clauses the controller has lost.

    ``state`` maps the plain field names ``claimed_mutation_domain_id``,
    ``authoritative_mutation_domain_id``, ``claimed_lease_epoch``, and
    ``authoritative_lease_epoch`` to the values involved (``None`` when the controller claims no
    identity/epoch, or when no authoritative value exists).

    Returns the lost-lease clauses in fixed clause order (an empty tuple when the controller
    still owns the lease for the domain):

    - ``DOMAIN_IDENTITY_LOST`` when the claimed mutation-domain identity no longer matches the
      authoritative repository mutation domain identity (absent or mismatched);
    - ``LEASE_EPOCH_LOST`` when the claimed lease epoch no longer matches the authoritative
      current lease epoch (absent, stale, or mismatched).

    A caller receiving a non-empty result SHALL NOT perform repository mutations for that domain
    (ARCH-CTRL-006).
    """
    if not isinstance(state, Mapping):
        raise TypeError("repository mutation lease state must be a mapping of lease fields")
    lost: list[str] = []
    if not _domain_holds(state.get(_FIELD_CLAIMED_DOMAIN), state.get(_FIELD_AUTHORITATIVE_DOMAIN)):
        lost.append(DOMAIN_IDENTITY_LOST)
    if not _epoch_holds(state.get(_FIELD_CLAIMED_EPOCH), state.get(_FIELD_AUTHORITATIVE_EPOCH)):
        lost.append(LEASE_EPOCH_LOST)
    return tuple(lost)
