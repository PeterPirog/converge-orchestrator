"""RED lock test for INV-MATERIALIZE-001: terminal-content retrievability.

INV-MATERIALIZE-001 (CONVERGE_ORCHESTRATOR_2_0_SOURCE_OF_TRUTH.md:L173 [4. Global invariants]):
Terminal convergence SHALL NOT depend on Git-native external content that exists only in a local
cache, prior worktree, developer machine, model sandbox, CI cache, or another undeclared source
and cannot be retrieved from the external source identity recorded for the canonical repository
revision.

The codebase has no primitive that decides whether Git-native external content that terminal
convergence depends on is retrievable from the external source identity recorded for the
canonical repository revision. These deterministic offline tests therefore lock the not-yet-
existing pure primitive ``converge_orchestrator.compliance.ComplianceEngine
.terminal_content_retrievable``:

- against the old implementation the class has no such symbol, so every test fails with the
  literal marker ``MATERIALIZE001_TERMINAL_CONTENT_MISSING``;
- once the primitive is added, the same tests must pass. It takes the external source identity
  recorded for the canonical repository revision (``None`` when none is recorded), the identity
  declared for the Git-native external content (``None`` when the content is undeclared), a flag
  for whether the content can be retrieved from the recorded external source identity, and the
  set of sources the content exists only in (empty when the content is not confined to
  local/undeclared sources), and returns a boolean:

  * retrievability disjunct -- content is admitted (True) when it can be retrieved from the
    external source identity recorded for the canonical repository revision;
  * declared-identity disjunct -- content is admitted (True) when its declared identity matches
    the recorded external source identity;
  * fail-closed -- content that exists only in a local cache, prior worktree, developer machine,
    model sandbox, CI cache, or another undeclared source and cannot be retrieved from the
    recorded external source identity is denied (False); content is also denied (False) when no
    external source identity is recorded for the canonical repository revision.

No network, model, provider, subprocess, git, or filesystem side effects: the checks are pure,
deterministic in-memory operations over plain values and collections.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import cast

import converge_orchestrator.compliance as compliance

_MISSING = "MATERIALIZE001_TERMINAL_CONTENT_MISSING"
_NAME = "terminal_content_retrievable"

# A pure terminal-content retrievability predicate:
# (recorded_source_identity, declared_source_identity, retrievable_from_recorded_source,
#  existing_only_in) -> bool.
Retrievability = Callable[..., bool]


def _primitive() -> Retrievability:
    """Resolve the INV-MATERIALIZE-001 primitive, failing with the RED marker if it is absent.

    Every test routes through this helper first, so against the old tree (where
    ``ComplianceEngine`` has no ``terminal_content_retrievable`` symbol) each test fails with
    the literal ``MATERIALIZE001_TERMINAL_CONTENT_MISSING`` marker rather than an opaque
    AttributeError.
    """
    method = getattr(compliance.ComplianceEngine, _NAME, None)
    assert method is not None, (
        f"{_MISSING}: converge_orchestrator.compliance.ComplianceEngine.{_NAME} is not defined; "
        "INV-MATERIALIZE-001 requires a deterministic, in-memory terminal-content "
        "retrievability predicate that admits Git-native external content only when it can "
        "be retrieved from the external source identity recorded for the canonical repository "
        "revision (or its declared identity matches that recorded identity), and fails closed "
        "for content that exists only in a local cache, prior worktree, developer machine, "
        "model sandbox, CI cache, or another undeclared source."
    )
    return cast(Retrievability, method)


def test_content_retrievable_from_recorded_source_admitted() -> None:
    """Retrievability disjunct: content retrievable from the recorded identity is admitted."""
    primitive = _primitive()
    # The content is retrievable from the external source identity recorded for the canonical
    # repository revision, so terminal convergence may depend on it even though no identity is
    # declared on the content itself and nothing confines it to local sources.
    result = primitive(
        recorded_source_identity="git:canonical-revision-1",
        declared_source_identity=None,
        retrievable_from_recorded_source=True,
        existing_only_in=frozenset(),
    )
    assert result is True


def test_declared_identity_matching_recorded_identity_admitted() -> None:
    """Declared-identity disjunct: a declared identity matching the recorded one is admitted."""
    primitive = _primitive()
    # The content's declared identity matches the external source identity recorded for the
    # canonical repository revision, so it is admitted even when no direct retrievability fact
    # is asserted.
    result = primitive(
        recorded_source_identity="git:canonical-revision-1",
        declared_source_identity="git:canonical-revision-1",
        retrievable_from_recorded_source=False,
        existing_only_in=frozenset(),
    )
    assert result is True


def test_content_only_in_local_cache_fails_closed() -> None:
    """Fail-closed: content existing only in a local cache is denied."""
    primitive = _primitive()
    result = primitive(
        recorded_source_identity="git:canonical-revision-1",
        declared_source_identity=None,
        retrievable_from_recorded_source=False,
        existing_only_in=frozenset({"local-cache"}),
    )
    assert result is False


def test_content_only_in_prior_worktree_fails_closed() -> None:
    """Fail-closed: content existing only in a prior worktree is denied."""
    primitive = _primitive()
    result = primitive(
        recorded_source_identity="git:canonical-revision-1",
        declared_source_identity=None,
        retrievable_from_recorded_source=False,
        existing_only_in=frozenset({"prior-worktree"}),
    )
    assert result is False


def test_content_only_in_developer_machine_fails_closed() -> None:
    """Fail-closed: content existing only in a developer machine is denied."""
    primitive = _primitive()
    result = primitive(
        recorded_source_identity="git:canonical-revision-1",
        declared_source_identity=None,
        retrievable_from_recorded_source=False,
        existing_only_in=frozenset({"developer-machine"}),
    )
    assert result is False


def test_content_only_in_model_sandbox_fails_closed() -> None:
    """Fail-closed: content existing only in a model sandbox is denied."""
    primitive = _primitive()
    result = primitive(
        recorded_source_identity="git:canonical-revision-1",
        declared_source_identity=None,
        retrievable_from_recorded_source=False,
        existing_only_in=frozenset({"model-sandbox"}),
    )
    assert result is False


def test_content_only_in_ci_cache_fails_closed() -> None:
    """Fail-closed: content existing only in a CI cache is denied."""
    primitive = _primitive()
    result = primitive(
        recorded_source_identity="git:canonical-revision-1",
        declared_source_identity=None,
        retrievable_from_recorded_source=False,
        existing_only_in=frozenset({"ci-cache"}),
    )
    assert result is False


def test_content_from_undeclared_source_fails_closed() -> None:
    """Fail-closed: content whose declared source is undeclared or mismatched is denied."""
    primitive = _primitive()
    # The content is declared against an undeclared source that differs from the identity
    # recorded for the canonical repository revision, and it cannot be retrieved from that
    # recorded identity, so terminal convergence must not depend on it.
    result = primitive(
        recorded_source_identity="git:canonical-revision-1",
        declared_source_identity="git:undeclared-source-9",
        retrievable_from_recorded_source=False,
        existing_only_in=frozenset({"undeclared-source"}),
    )
    assert result is False


def test_no_recorded_identity_fails_closed() -> None:
    """Fail-closed: with no recorded identity, no declared/None identity pair may admit."""
    primitive = _primitive()
    # No external source identity is recorded for the canonical repository revision and the
    # content declares none either; the absence of a recorded identity must fail closed rather
    # than admit by a vacuous identity match.
    result = primitive(
        recorded_source_identity=None,
        declared_source_identity=None,
        retrievable_from_recorded_source=False,
        existing_only_in=frozenset(),
    )
    assert result is False
