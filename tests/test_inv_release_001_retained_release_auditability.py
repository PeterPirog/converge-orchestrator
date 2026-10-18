"""RED lock test for INV-RELEASE-001: ref-independent retained-release auditability.

INV-RELEASE-001 (CONVERGE_ORCHESTRATOR_2_0_SOURCE_OF_TRUTH.md:L176 [4. Global invariants]):
A retained historical release SHALL remain independently auditable after later branch movement,
force-update, ref deletion, repository maintenance, or ordinary remote garbage collection and
SHALL NOT depend on the released commit remaining reachable from a mutable Git ref.

The codebase has no primitive that decides, for a retained historical release, whether every
retained audit element is anchored by a ref-independent immutable source rather than a mutable
Git ref. These deterministic offline tests therefore lock the not-yet-existing pure primitive
``converge_orchestrator.git.RetainedReleaseAuditability``:

- against the old implementation the module has no such symbol, so every test fails with the
  literal marker ``RELEASE001_RETAINED_RELEASE_AUDITABILITY_MISSING``;
- once the primitive is added, the same tests must pass. It is constructed from the released
  commit (``released_commit``), the retained audit element ids (``retained_audits``), the
  retention sources each element is anchored by (``retention_sources``), and the ref events that
  have since occurred (``ref_events``, defaulting to ``()``), where each retention source records
  its ``kind`` and whether it is a mutable Git ref (``mutable``). It reports ``auditable``
  (``True`` only when every retained audit element has at least one non-mutable, ref-independent
  retention source) and ``ref_dependent`` (a stably ordered, sorted tuple of exactly the retained
  audit element ids that have no ref-independent anchor):

  * fail-closed -- a retained audit element anchored only by a mutable Git ref (a branch or tag
    ref) -- or one with no retention source at all -- keeps ``auditable`` False and appears in
    ``ref_dependent``;
  * ref-independent disjunct -- a retained audit element anchored by at least one non-mutable,
    ref-independent immutable source is admitted (``auditable`` True, absent from
    ``ref_dependent``) even when it is also anchored by a mutable Git ref;
  * ref-event invariance -- later branch movement, force-update, ref deletion, repository
    maintenance, and ordinary remote garbage collection events do not change ``auditable`` for a
    release whose retained elements are each anchored by a ref-independent source.

No network, model, provider, subprocess, git, or filesystem side effects: the checks are pure,
deterministic in-memory operations over plain values and collections.
"""

from __future__ import annotations

import converge_orchestrator.git as git

_MISSING = "RELEASE001_RETAINED_RELEASE_AUDITABILITY_MISSING"
_NAME = "RetainedReleaseAuditability"


def _auditability_class() -> type:
    """Resolve the INV-RELEASE-001 primitive, failing with the RED marker if it is absent.

    Every test routes through this helper first, so against the old tree (where
    ``converge_orchestrator.git`` has no ``RetainedReleaseAuditability`` symbol) each test fails
    with the literal ``RELEASE001_RETAINED_RELEASE_AUDITABILITY_MISSING`` marker rather than an
    opaque AttributeError.
    """
    cls = getattr(git, _NAME, None)
    assert cls is not None, (
        f"{_MISSING}: converge_orchestrator.git.{_NAME} is not defined; "
        "INV-RELEASE-001 requires a pure, deterministic, in-memory retained-release "
        "auditability primitive that reports ``auditable`` True only when every retained audit "
        "element is anchored by at least one non-mutable, ref-independent immutable retention "
        "source, fails closed for any element anchored only by a mutable Git ref (or with no "
        "retention source), exposes ``ref_dependent`` as a stably ordered (sorted) tuple of "
        "exactly the retained audit element ids with no ref-independent anchor, and whose "
        "decision is unchanged by later branch movement, force-update, ref deletion, repository "
        "maintenance, or ordinary remote garbage collection."
    )
    return cls


def _source(kind: str, *, mutable: bool) -> dict[str, object]:
    """Build a plain retention source record: its category and whether it is a mutable Git ref."""
    return {"kind": kind, "mutable": mutable}


def _ref_independent(kind: str = "immutable_object_store") -> dict[str, object]:
    """A non-mutable, ref-independent immutable retention source (not a mutable Git ref)."""
    return _source(kind, mutable=False)


def _mutable_ref(kind: str = "branch_ref") -> dict[str, object]:
    """A mutable Git ref retention source (e.g. a branch or tag ref)."""
    return _source(kind, mutable=True)


def _ref_event(kind: str) -> dict[str, object]:
    """Build a plain ref event record: the kind of ref activity that has since occurred."""
    return {"kind": kind}


def test_mutable_git_ref_only_is_not_auditable() -> None:
    """Fail-closed: an element retained only via a mutable branch ref is not auditable."""
    audit = _auditability_class()(
        released_commit="release-abc123",
        retained_audits=("audit-1",),
        retention_sources={"audit-1": [_mutable_ref("branch_ref")]},
    )
    assert audit.auditable is False
    assert audit.ref_dependent == ("audit-1",)


def test_mutable_tag_ref_only_is_not_auditable() -> None:
    """Fail-closed: an element retained only via a mutable tag ref is not auditable."""
    audit = _auditability_class()(
        released_commit="release-abc123",
        retained_audits=("audit-1",),
        retention_sources={"audit-1": [_mutable_ref("tag_ref")]},
    )
    assert audit.auditable is False
    assert audit.ref_dependent == ("audit-1",)


def test_no_retention_source_fails_closed() -> None:
    """Fail-closed: a retained element with no retention source at all is not auditable."""
    audit = _auditability_class()(
        released_commit="release-abc123",
        retained_audits=("audit-1",),
        retention_sources={},
    )
    assert audit.auditable is False
    assert audit.ref_dependent == ("audit-1",)


def test_ref_independent_source_admits_element_also_in_mutable_ref() -> None:
    """Ref-independent disjunct: a non-mutable source admits an element also in a mutable ref."""
    audit = _auditability_class()(
        released_commit="release-abc123",
        retained_audits=("audit-1",),
        retention_sources={
            "audit-1": [
                _ref_independent(),
                _mutable_ref("branch_ref"),
                _mutable_ref("tag_ref"),
            ],
        },
    )
    assert audit.auditable is True
    assert audit.ref_dependent == ()


def test_auditable_when_every_element_has_ref_independent_source() -> None:
    """Positive control: every retained element is anchored by a ref-independent source."""
    audit = _auditability_class()(
        released_commit="release-abc123",
        retained_audits=("audit-1", "audit-2"),
        retention_sources={
            "audit-1": [_ref_independent()],
            "audit-2": [_ref_independent(), _mutable_ref("tag_ref")],
        },
    )
    assert audit.auditable is True
    assert audit.ref_dependent == ()


def test_ref_dependent_is_stably_sorted_tuple() -> None:
    """ref_dependent is the sorted tuple of exactly the elements with no independent anchor."""
    audit = _auditability_class()(
        released_commit="release-abc123",
        retained_audits=("mid", "zeta", "alpha"),
        retention_sources={
            "mid": [_mutable_ref("branch_ref")],
            "zeta": [_ref_independent()],
            "alpha": [_mutable_ref("tag_ref")],
        },
    )
    assert audit.auditable is False
    # "zeta" is ref-independent and therefore excluded; "alpha" and "mid" remain, in sorted order.
    assert audit.ref_dependent == ("alpha", "mid")


def test_ref_events_do_not_change_auditable_for_ref_independent_release() -> None:
    """The five ref-event kinds do not change auditable for a ref-independent release."""
    audit = _auditability_class()(
        released_commit="release-abc123",
        retained_audits=("audit-1",),
        retention_sources={"audit-1": [_ref_independent()]},
        ref_events=(
            _ref_event("branch_movement"),
            _ref_event("force_update"),
            _ref_event("ref_deletion"),
            _ref_event("repository_maintenance"),
            _ref_event("remote_garbage_collection"),
        ),
    )
    assert audit.auditable is True
    assert audit.ref_dependent == ()

    # The identical release is auditable with no ref events at all.
    audit_no_events = _auditability_class()(
        released_commit="release-abc123",
        retained_audits=("audit-1",),
        retention_sources={"audit-1": [_ref_independent()]},
    )
    assert audit_no_events.auditable is True
    assert audit_no_events.ref_dependent == ()
