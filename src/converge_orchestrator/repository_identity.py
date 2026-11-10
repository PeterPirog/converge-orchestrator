"""REQ-REPO-005: separate nested repository identity authority guard.

Pure, offline predicate for the requirement REQ-REPO-005
(CONVERGE_ORCHESTRATOR_2_0_SOURCE_OF_TRUTH.md:L281 [9. Repository modes,
greenfield bootstrap, and brownfield discovery]): nested Git repositories,
submodule checkouts, linked worktrees, and external repository mounts inside a
candidate tree SHALL remain separate repository identities; editing their
internal contents SHALL require explicit project authority rather than being
implied by authority over the primary target repository.

``evaluate_repository_identity_authority`` inspects a plain mapping describing
the identity of the content being mutated and the set of nested identities the
project has explicitly been granted authority over. It returns ``None`` when the
mutation is admitted and the fixed rejection marker when it is not.

There is no network, model, provider, subprocess, git command, filesystem, or
other side-effecting access in this module.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from typing import cast

#: REQ-REPO-005 fixed rejection marker returned when mutation is not admitted.
REPOSITORY_IDENTITY_REJECTED = "REQ-REPO-005 nested repository identity authority rejected"

_KIND_PRIMARY = "primary_repository"
_KIND_NESTED = "nested_repository"
_KIND_SUBMODULE = "submodule_checkout"
_KIND_WORKTREE = "linked_worktree"
_KIND_EXTERNAL = "external_mount"

_NESTED_KINDS = frozenset(
    {_KIND_NESTED, _KIND_SUBMODULE, _KIND_WORKTREE, _KIND_EXTERNAL}
)

_FIELD_KIND = "target_identity_kind"
_FIELD_ID = "nested_identity_id"
_FIELD_GRANTS = "authority_grants"

#: Authority-grant collections supported for exact identity-id membership.  Strings and bytes are
#: excluded because ``in`` on those types performs substring matching, which would let a malformed
#: grant value fabricate an admission (INV-CORE-003).
_GRANT_COLLECTION_TYPES = (set, frozenset, tuple, list)


def _is_grant_collection(value: object) -> bool:
    """Return ``True`` only for a non-string, non-byte, non-mapping collection of identity ids."""
    return isinstance(value, _GRANT_COLLECTION_TYPES)


def evaluate_repository_identity_authority(state: Mapping[str, object]) -> str | None:
    """Admit a mutation only when project authority covers the target repository identity.

    ``state`` is a plain mapping with:

      - ``target_identity_kind``: ``"primary_repository"`` or one of the nested kinds;
      - ``nested_identity_id``: the identity of the nested content (ignored for the primary);
      - ``authority_grants``: a collection of nested identity ids the project has been
        explicitly granted authority over.

    Returns ``None`` when mutation is admitted, otherwise the fixed marker
    ``REPOSITORY_IDENTITY_REJECTED``. The guard fails closed: an unknown kind, a nested identity
    without an exact grant, or malformed authority state is rejected.

    A non-mapping ``state`` raises ``TypeError`` in keeping with the repo's sibling pure-
    predicate guards; all other malformed shapes return ``REPOSITORY_IDENTITY_REJECTED``.
    """
    if not isinstance(state, Mapping):
        raise TypeError(
            "repository identity state must be a mapping of repository-identity authority fields"
        )
    kind = state.get(_FIELD_KIND)
    if kind == _KIND_PRIMARY:
        return None
    try:
        is_nested = kind in _NESTED_KINDS
    except TypeError:
        # Malformed, unhashable kind value; fail closed rather than letting a set lookup raise.
        is_nested = False
    if not is_nested:
        return REPOSITORY_IDENTITY_REJECTED
    identity_id = state.get(_FIELD_ID)
    if identity_id is None:
        return REPOSITORY_IDENTITY_REJECTED
    grants = state.get(_FIELD_GRANTS, ())
    if not _is_grant_collection(grants):
        return REPOSITORY_IDENTITY_REJECTED
    try:
        if identity_id in cast("Iterable[object]", grants):
            return None
    except TypeError:
        # ``identity_id`` is unhashable relative to the grant collection; treat as unknown identity.
        return REPOSITORY_IDENTITY_REJECTED
    return REPOSITORY_IDENTITY_REJECTED
