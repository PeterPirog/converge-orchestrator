"""REQ-DATA-004: digest-only outbound invocation evidence record builder.

REQ-DATA-004 (CONVERGE_ORCHESTRATOR_2_0_SOURCE_OF_TRUTH.md:L235
[6. Project configuration and secret sources]): Outbound model or tool evidence
SHALL record provider trust class, endpoint/profile identity, role identity, and
authoritative context-envelope digest without persisting restricted payload
contents solely for observability.

These offline, deterministic tests pin the pure builder
``converge_orchestrator.evidence.build_outbound_evidence_record``:

* it returns a dict exposing exactly ``provider_trust_class``,
  ``endpoint_identity``, ``profile_identity``, ``role_identity``, and
  ``context_envelope_sha256`` in stable sorted key order;
* it computes ``context_envelope_sha256`` as the SHA-256 hex digest of the
  ``context_envelope`` bytes;
* the JSON-serialized record never contains the raw context-envelope bytes or
  the restricted payload text;
* changing only the restricted payload while keeping the context envelope
  constant leaves the record unchanged;
* changing the context-envelope bytes changes the recorded digest;
* missing, blank, non-string identity fields or a non-bytes context envelope fail
  closed with ``ValueError``.

The builder does not exist yet, so every call below fails with the literal TDD
RED evidence marker ``REQ-DATA-004-OUTBOUND-EVIDENCE-RECORD-BUILDER-MISSING``
until the digest-only record builder is added. No filesystem, network, model,
provider, subprocess, git, or other nondeterministic access occurs.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any

import pytest

# TDD RED evidence marker (tdd.expected_failure_pattern): carried by every
# failure below until the pure builder
# converge_orchestrator.evidence.build_outbound_evidence_record is added. It is
# a literal, unique string that does not appear anywhere else in the repository,
# so its appearance in a test run is deterministic RED evidence.
_BUILDER_MISSING = "REQ-DATA-004-OUTBOUND-EVIDENCE-RECORD-BUILDER-MISSING"


# Expected outbound evidence record field set.
_RECORD_KEYS = {
    "provider_trust_class",
    "endpoint_identity",
    "profile_identity",
    "role_identity",
    "context_envelope_sha256",
}


def _builder():
    """Return the builder, failing closed with the literal RED marker when absent."""
    try:
        from converge_orchestrator.evidence import build_outbound_evidence_record
    except ImportError as exc:
        raise AssertionError(_BUILDER_MISSING) from exc
    return build_outbound_evidence_record


def _build(
    provider_trust_class: Any = "trusted",
    endpoint_identity: Any = "endpoint-1",
    profile_identity: Any = "profile-1",
    role_identity: Any = "role-1",
    context_envelope: Any = b"authoritative-context-envelope",
    restricted_payload: bytes | None = None,
) -> dict[str, Any]:
    """Invoke the digest-only outbound evidence record builder."""
    return _builder()(
        provider_trust_class,
        endpoint_identity,
        profile_identity,
        role_identity,
        context_envelope,
        restricted_payload=restricted_payload,
    )


def test_valid_record_exposes_exactly_five_sorted_fields() -> None:
    record = _build(
        provider_trust_class="local",
        endpoint_identity="ep-model-7",
        profile_identity="profile-fast",
        role_identity="role-reviewer",
        context_envelope=b"envelope-A",
    )
    assert isinstance(record, dict)
    assert set(record.keys()) == _RECORD_KEYS
    assert list(record.keys()) == sorted(_RECORD_KEYS)


def test_context_envelope_sha256_is_sha256_digest() -> None:
    envelope = b"authoritative-context-envelope-bytes"
    record = _build(context_envelope=envelope)
    assert record["context_envelope_sha256"] == hashlib.sha256(envelope).hexdigest()


def test_changing_context_envelope_bytes_changes_digest() -> None:
    record_a = _build(context_envelope=b"envelope-A")
    record_b = _build(context_envelope=b"envelope-B")
    assert record_a["context_envelope_sha256"] != record_b["context_envelope_sha256"]
    # The only field that differs is the digest.
    assert {
        k: v
        for k, v in record_a.items()
        if k != "context_envelope_sha256"
    } == {
        k: v
        for k, v in record_b.items()
        if k != "context_envelope_sha256"
    }


def test_serialized_record_never_contains_envelope_or_payload_text() -> None:
    envelope = b"authoritative-envelope-containing-secret-payload-text"
    payload = b"super-secret-restricted-payload-42"
    record = _build(
        provider_trust_class="trusted",
        endpoint_identity="ep-gateway-3",
        profile_identity="profile-default",
        role_identity="role-writer",
        context_envelope=envelope,
        restricted_payload=payload,
    )
    serialized = json.dumps(record, sort_keys=True, separators=(",", ":"))
    assert envelope.decode("utf-8") not in serialized
    assert payload.decode("utf-8") not in serialized
    # Only the digest appears.
    assert record["context_envelope_sha256"] in serialized


def test_changing_only_restricted_payload_leaves_record_unchanged() -> None:
    envelope = b"authoritative-context-envelope"
    payload_a = b"restricted-payload-A"
    payload_b = b"restricted-payload-B"
    record_a = _build(
        provider_trust_class="trusted",
        endpoint_identity="ep-1",
        profile_identity="profile-1",
        role_identity="role-1",
        context_envelope=envelope,
        restricted_payload=payload_a,
    )
    record_b = _build(
        provider_trust_class="trusted",
        endpoint_identity="ep-1",
        profile_identity="profile-1",
        role_identity="role-1",
        context_envelope=envelope,
        restricted_payload=payload_b,
    )
    assert record_a == record_b


def test_record_keys_are_stable_across_builds() -> None:
    record_a = _build(
        provider_trust_class="local",
        endpoint_identity="ep-a",
        profile_identity="profile-a",
        role_identity="role-a",
        context_envelope=b"envelope",
    )
    record_b = _build(
        provider_trust_class="local",
        endpoint_identity="ep-a",
        profile_identity="profile-a",
        role_identity="role-a",
        context_envelope=b"envelope",
    )
    assert list(record_a.keys()) == list(record_b.keys()) == sorted(_RECORD_KEYS)
    assert json.dumps(record_a, sort_keys=True) == json.dumps(record_b, sort_keys=True)


@pytest.mark.parametrize(
    "bad_value",
    [None, "", "   ", 123, []],
    ids=["none", "empty", "whitespace", "int", "list"],
)
def test_provider_trust_class_must_be_non_blank_string(bad_value: Any) -> None:
    with pytest.raises(ValueError):
        _build(provider_trust_class=bad_value)


@pytest.mark.parametrize(
    "bad_value",
    [None, "", "   ", 123, []],
    ids=["none", "empty", "whitespace", "int", "list"],
)
def test_endpoint_identity_must_be_non_blank_string(bad_value: Any) -> None:
    with pytest.raises(ValueError):
        _build(endpoint_identity=bad_value)


@pytest.mark.parametrize(
    "bad_value",
    [None, "", "   ", 123, []],
    ids=["none", "empty", "whitespace", "int", "list"],
)
def test_profile_identity_must_be_non_blank_string(bad_value: Any) -> None:
    with pytest.raises(ValueError):
        _build(profile_identity=bad_value)


@pytest.mark.parametrize(
    "bad_value",
    [None, "", "   ", 123, []],
    ids=["none", "empty", "whitespace", "int", "list"],
)
def test_role_identity_must_be_non_blank_string(bad_value: Any) -> None:
    with pytest.raises(ValueError):
        _build(role_identity=bad_value)


@pytest.mark.parametrize(
    "bad_envelope",
    [None, "not-bytes", 123, [b"bytes-in-list"], {"bytes": b"in-dict"}],
    ids=["none", "string", "int", "list", "dict"],
)
def test_context_envelope_must_be_bytes(bad_envelope: Any) -> None:
    with pytest.raises(ValueError):
        _build(context_envelope=bad_envelope)
