"""RED lock test for ARCH-CTRL-005: fencing-aware mutation authority guard.

ARCH-CTRL-005 (CONVERGE_ORCHESTRATOR_2_0_SOURCE_OF_TRUTH.md:L243 [7. Deterministic architecture
and agent roles]): In a deployment where mutation authority can transfer while an earlier
controller may later resume, canonical-repository and remote mutations SHALL be mediated by a
fencing-aware authority that validates the current epoch as part of accepting the mutation, or by
an epoch-scoped capability that becomes unusable after transfer; possession of a generic long-lived
mutation credential by a stale controller SHALL NOT be sufficient to publish.

These deterministic offline tests lock the not-yet-existing pure predicate
``converge_orchestrator.mutation_authority.can_publish_mutation``:

- against the old implementation the module/symbol does not exist, so every test fails with the
  literal marker ``ARCHCTRL005_FENCING_AWARE_MUTATION_AUTHORITY_MISSING``;
- once the primitive is added, the same tests must pass. The predicate takes a mutation scope
  (``"canonical_repository"`` or ``"remote"``) and a plain mapping describing the controller's
  claimed mutation-fencing epochs and the authoritative current epochs. It returns ``True`` only
  when the claimed epoch capability exactly matches the authoritative current epoch for every
  required fence, delegating to ``converge_orchestrator.fencing.evaluate_mutation_fence``
  semantics. All other inputs fail closed and return ``False``.

No network, model, provider, subprocess, git command, or filesystem side effect is exercised in the
tested path.
"""

from __future__ import annotations

import importlib
from collections.abc import Callable
from typing import cast

_MISSING = "ARCHCTRL005_FENCING_AWARE_MUTATION_AUTHORITY_MISSING"
_MODULE = "converge_orchestrator.mutation_authority"
_NAME = "can_publish_mutation"

_SCOPE_CANONICAL = "canonical_repository"
_SCOPE_REMOTE = "remote"

# Pure guard: (scope, plain mutation-fence mapping) -> bool admission decision.
Guard = Callable[[str, object], bool]


def _guard() -> Guard:
    """Resolve the ARCH-CTRL-005 primitive, failing with the RED marker if it is absent."""
    try:
        module = importlib.import_module(_MODULE)
    except ModuleNotFoundError as exc:
        raise AssertionError(
            f"{_MISSING}: {_MODULE}.{_NAME} is not defined; "
            "ARCH-CTRL-005 requires a deterministic, offline, fencing-aware mutation-authority "
            "guard that validates the current epoch before admitting canonical-repository or "
            "remote mutations."
        ) from exc
    guard = getattr(module, _NAME, None)
    assert callable(guard), (
        f"{_MISSING}: {_MODULE}.{_NAME} is not defined; "
        "ARCH-CTRL-005 requires a deterministic mutation-authority predicate."
    )
    return cast(Guard, guard)


def _state(
    *,
    claimed_run: object = 7,
    claimed_project: object = 42,
    current_run: object = 7,
    current_project: object = 42,
) -> dict[str, object]:
    """Build a plain mutation-fence mapping with no coupling to controller state."""
    return {
        "claimed_run_fence_epoch": claimed_run,
        "claimed_project_fence_epoch": claimed_project,
        "current_run_fence_epoch": current_run,
        "current_project_fence_epoch": current_project,
    }


def test_canonical_repository_admitted_when_epoch_capability_matches() -> None:
    guard = _guard()
    # The controller's claimed run and project epochs both match the authoritative current
    # epochs, so the canonical-repository mutation is admitted.
    assert guard(_SCOPE_CANONICAL, _state()) is True


def test_remote_admitted_when_epoch_capability_matches() -> None:
    guard = _guard()
    # The same matching capability also admits the remote mutation scope.
    assert guard(_SCOPE_REMOTE, _state()) is True


def test_canonical_repository_denied_when_run_epoch_is_stale() -> None:
    guard = _guard()
    # A stale claimed run epoch means the controller no longer owns the current run fence.
    assert guard(_SCOPE_CANONICAL, _state(claimed_run=5, current_run=10)) is False


def test_remote_denied_when_run_epoch_is_stale() -> None:
    guard = _guard()
    # The remote mutation scope is denied by the same stale run epoch.
    assert guard(_SCOPE_REMOTE, _state(claimed_run=5, current_run=10)) is False


def test_canonical_repository_denied_when_project_epoch_is_mismatched() -> None:
    guard = _guard()
    # A project epoch that differs from the authoritative current one loses that fence.
    assert guard(_SCOPE_CANONICAL, _state(claimed_project=43, current_project=42)) is False


def test_remote_denied_when_project_epoch_is_absent() -> None:
    guard = _guard()
    # An absent claimed project epoch cannot prove current-project ownership.
    assert guard(_SCOPE_REMOTE, _state(claimed_project=None, current_project=42)) is False


def test_canonical_repository_denied_when_no_capability_proven() -> None:
    guard = _guard()
    # An empty mapping provides no claimed epoch capability at all, so the guard fails closed.
    assert guard(_SCOPE_CANONICAL, {}) is False


def test_remote_denied_when_no_capability_proven() -> None:
    guard = _guard()
    # No capability proven also denies the remote mutation scope.
    assert guard(_SCOPE_REMOTE, {}) is False
