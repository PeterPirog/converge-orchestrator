"""RED lock test for INV-EVID-002: content-integrity-bound retained convergence evidence.

INV-EVID-002 (CONVERGE_ORCHESTRATOR_2_0_SOURCE_OF_TRUTH.md:L167 [4. Global invariants]):
Evidence that contributes to integration, compliance PASS, acceptance, or release convergence
SHALL be content-integrity-bound so later replacement, truncation, or mutation of the retained
artifact is detected before that evidence is reused.

The codebase has no primitive that binds retained evidence bytes to their exact SHA-256 digest
and byte size and detects later replacement, truncation, or mutation. These deterministic
offline tests therefore lock the not-yet-existing primitive
``converge_orchestrator.evidence.ContentIntegrityBinding``:

- against the old implementation the module has no such symbol, so every test fails with the
  literal marker ``EVID002_CONTENT_INTEGRITY_BINDING_MISSING``;
- once the primitive is added, the same tests must pass: a binding reports the exact SHA-256
  digest and byte size of the retained content it bound, and its verification is ``True`` for
  byte-identical retained content and ``False`` on replacement, truncation, or mutation.

Digests are SHA-256 hex digests, consistent with the existing evidence code (e.g.
``hashlib.sha256(...).hexdigest()``). No network, model, provider, subprocess, or filesystem side
effects: binding and verification are pure in-memory operations.
"""

from __future__ import annotations

import hashlib

import converge_orchestrator.evidence as evidence

_MISSING = "EVID002_CONTENT_INTEGRITY_BINDING_MISSING"


def _sha256(value: bytes) -> str:
    """Deterministic SHA-256 content identity digest (64 lowercase hex chars)."""
    return hashlib.sha256(value).hexdigest()


def _binding() -> type:
    """Resolve the INV-EVID-002 primitive, failing with the RED marker if it is absent."""
    primitive = getattr(evidence, "ContentIntegrityBinding", None)
    assert primitive is not None, (
        f"{_MISSING}: converge_orchestrator.evidence.ContentIntegrityBinding is not defined; "
        "INV-EVID-002 requires retained evidence to be content-integrity-bound so later "
        "replacement, truncation, or mutation of the retained artifact is detected before reuse."
    )
    return primitive


def test_binding_reports_exact_sha256_digest_and_byte_size() -> None:
    Bound = _binding()
    content = b'{"run_id":"run-1","task_id":"task-1","verdict":"PASS"}\n'
    bound = Bound(content)
    assert bound.content_sha256 == _sha256(content)
    assert bound.content_size == len(content)


def test_verify_true_for_byte_identical_retained_content() -> None:
    Bound = _binding()
    content = b"retained convergence evidence payload v1"
    bound = Bound(content)
    assert bound.verify(b"retained convergence evidence payload v1") is True


def test_verify_false_after_replacement_of_retained_artifact() -> None:
    Bound = _binding()
    original = b"retained convergence evidence payload v1"
    replacement = b"retained convergence evidence payload v2"
    assert _sha256(original) != _sha256(replacement)
    bound = Bound(original)
    assert bound.verify(replacement) is False


def test_verify_false_after_truncation_of_retained_artifact() -> None:
    Bound = _binding()
    original = b"0123456789abcdef"
    truncated = original[:8]
    assert len(truncated) != len(original)
    bound = Bound(original)
    assert bound.verify(truncated) is False


def test_verify_false_after_mutation_of_retained_artifact() -> None:
    Bound = _binding()
    original = b"retained convergence evidence payload v1"
    mutated = original.replace(b"v1", b"vX")
    assert mutated != original
    bound = Bound(original)
    assert bound.verify(mutated) is False
