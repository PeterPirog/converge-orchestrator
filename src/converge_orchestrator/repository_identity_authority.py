"""REQ-REPO-005: nested-repository identity authority gate.

Pure, offline primitive for REQ-REPO-005
(CONVERGE_ORCHESTRATOR_2_0_SOURCE_OF_TRUTH.md:L281): nested Git
repositories, submodules, linked worktrees, and external repository
mounts remain separate identities; editing internal contents requires
explicit project authority. No network, model, provider, subprocess, git, or filesystem I/O.
"""

from __future__ import annotations

from collections.abc import Mapping

PRIMARY_NOT_TRANSITIVE = "primary_target_authority_not_transitive_to_nested_identity"
GRANT_INVALID = "nested_identity_explicit_grant_missing_or_mismatched"
_FIELD_PRIMARY_TARGET_AUTHORITY = "primary_target_authority"
_FIELD_NESTED_BOUNDARIES = "nested_boundaries"
_FIELD_PROJECT_AUTHORITY_GRANTS = "project_authority_grants"


def evaluate_nested_repository_identity_authority(
    state: Mapping[str, object],
) -> Mapping[str, tuple[str, ...]]:
    if not isinstance(state, Mapping):
        raise TypeError("nested repository identity authority state must be a mapping")
    boundaries = state.get(_FIELD_NESTED_BOUNDARIES)
    if boundaries is None:
        boundaries = {}
    elif not isinstance(boundaries, Mapping):
        raise TypeError("nested_boundaries must be a mapping")
    grants = state.get(_FIELD_PROJECT_AUTHORITY_GRANTS)
    if grants is None:
        grants = {}
    elif not isinstance(grants, Mapping):
        raise TypeError("project_authority_grants must be a mapping")
    primary_present = state.get(_FIELD_PRIMARY_TARGET_AUTHORITY) not in (None, "")
    result: dict[str, tuple[str, ...]] = {}
    for boundary in sorted(boundaries):
        grant = grants.get(boundary)
        if isinstance(grant, str) and grant != "" and grant == boundaries[boundary]:
            result[boundary] = ()
        else:
            result[boundary] = tuple(
                ([PRIMARY_NOT_TRANSITIVE] if primary_present else []) + [GRANT_INVALID]
            )
    return result
