"""REQ-CONFIG-016: campaign-policy identity pinning and successor mismatch guard.

REQ-CONFIG-016 (CONVERGE_ORCHESTRATOR_2_0_SOURCE_OF_TRUTH.md:L214
[6. Project configuration and secret sources]): a convergence campaign SHALL pin a
campaign-policy identity covering its Source-of-Truth digest, integration line,
cumulative resource envelope, interaction policy, and the minimum gate, review,
security, evidence, and remote-integration semantics that successor runs are not
permitted to weaken.

The codebase has no campaign-policy identity primitive. These deterministic offline
tests therefore lock the not-yet-existing pure primitives on
``converge_orchestrator.campaign``:

- ``campaign_policy_identity``: a pure, deterministic SHA-256 identity over a complete
  nine-component campaign-policy mapping. Against the old implementation the symbol is
  absent, so every test here fails with the literal marker
  ``REQ-CONFIG-016-CAMPAIGN-POLICY-IDENTITY-MISSING``.
- ``evaluate_campaign_policy_continuation``: a fail-closed successor guard that reports
  ``campaign_policy_identity_changed`` when a successor run's policy identity differs
  from the pinned campaign identity.

Once the primitives are added, the same tests must pass:

* ``campaign_policy_identity`` returns a stable 64-character lowercase SHA-256 hex
  digest for a complete nine-component campaign-policy mapping;
* the identity is identical for equal mappings regardless of key insertion order;
* changing any one of the nine required components changes the identity;
* a missing, ``None``, or blank required component fails closed with a ``ValueError``
  naming the offending component;
* ``evaluate_campaign_policy_continuation`` takes a mapping with a ``pinned`` and a
  ``successor`` campaign-policy mapping and reports no violations when the two identities
  match, and exactly ``campaign_policy_identity_changed`` when any covered component
  differs.

The identity is computed with standard-library hashing and canonical (sorted) JSON
serialization only, so it must not depend on mapping insertion order. No network, model,
provider, subprocess, git, or filesystem access occurs in the tested paths.
"""

from __future__ import annotations

import importlib
import re
from collections.abc import Callable, Iterable, Mapping
from typing import cast

import pytest

_MISSING = "REQ-CONFIG-016-CAMPAIGN-POLICY-IDENTITY-MISSING"
_MODULE = "converge_orchestrator.campaign"
_IDENTITY_NAME = "campaign_policy_identity"
_CONTINUATION_NAME = "evaluate_campaign_policy_continuation"
_CHANGED = "campaign_policy_identity_changed"

_SHA256_HEX = re.compile(r"^[0-9a-f]{64}$")

# The nine required campaign-policy components. The identity MUST NOT depend on mapping
# insertion order; this fixed order is used only to drive the parametrized component tests.
_COMPONENTS = (
    "source_of_truth_digest",
    "integration_line",
    "cumulative_resource_envelope",
    "interaction_policy",
    "minimum_gate",
    "minimum_review",
    "minimum_security",
    "minimum_evidence",
    "minimum_remote_integration",
)


def _resolve(name: str) -> object:
    """Resolve a REQ-CONFIG-016 primitive, failing with the RED marker if it is absent.

    Every test routes through this helper first, so against the old tree (where
    ``converge_orchestrator.campaign`` exposes no campaign-policy identity primitive) each
    test fails with the literal ``REQ-CONFIG-016-CAMPAIGN-POLICY-IDENTITY-MISSING`` marker
    rather than an opaque import error.
    """
    try:
        module = importlib.import_module(_MODULE)
    except ModuleNotFoundError as exc:
        raise AssertionError(
            f"{_MISSING}: {_MODULE}.{name} is not defined; REQ-CONFIG-016 requires a pure, "
            "deterministic, offline campaign-policy identity primitive."
        ) from exc
    primitive = getattr(module, name, None)
    assert callable(primitive), (
        f"{_MISSING}: {_MODULE}.{name} is not defined; REQ-CONFIG-016 requires a "
        "deterministic campaign-policy identity and a successor-continuation guard."
    )
    return primitive


def _identity() -> Callable[[Mapping[str, object]], str]:
    return cast("Callable[[Mapping[str, object]], str]", _resolve(_IDENTITY_NAME))


def _continuation() -> Callable[[Mapping[str, object]], Iterable[str]]:
    return cast("Callable[[Mapping[str, object]], Iterable[str]]", _resolve(_CONTINUATION_NAME))


def _policy() -> dict[str, object]:
    """A complete nine-component campaign-policy mapping with distinct non-blank values."""
    return {
        "source_of_truth_digest": "9f2c4e7a1b8d3065f4a2c9e7d8b01356a9c4f2d7e8b0a1c3",
        "integration_line": "main",
        "cumulative_resource_envelope": "envelope-2026-10",
        "interaction_policy": "interactive-approval",
        "minimum_gate": "quality-gates-full",
        "minimum_review": "two-reviewers",
        "minimum_security": "l4-hardening",
        "minimum_evidence": "evidence-closed",
        "minimum_remote_integration": "ci-plus-pr-checks",
    }


def test_identity_is_stable_64_char_lowercase_sha256_hex() -> None:
    compute = _identity()
    policy = _policy()
    digest = compute(policy)
    assert isinstance(digest, str), (
        f"{_MISSING}: campaign_policy_identity must return a hex digest string"
    )
    assert len(digest) == 64, (
        f"{_MISSING}: campaign_policy_identity must return a 64-character digest"
    )
    assert _SHA256_HEX.fullmatch(digest), (
        f"{_MISSING}: campaign_policy_identity must return a lowercase SHA-256 hex digest"
    )
    assert compute(policy) == digest, (
        f"{_MISSING}: campaign_policy_identity must be stable for an identical mapping"
    )


def test_identity_is_independent_of_key_insertion_order() -> None:
    compute = _identity()
    base = _policy()
    reference = compute(base)
    items = list(base.items())
    for reordered in (dict(reversed(items)), dict(reversed(items[4:] + items[:4]))):
        assert compute(reordered) == reference, (
            f"{_MISSING}: the identity must not depend on mapping key insertion order"
        )


@pytest.mark.parametrize("component", _COMPONENTS, ids=list(_COMPONENTS))
def test_changing_any_component_changes_identity(component: str) -> None:
    compute = _identity()
    reference = compute(_policy())
    mutated = _policy()
    mutated[component] = f"altered:{component}"
    assert compute(mutated) != reference, (
        f"{_MISSING}: changing {component!r} must change the campaign-policy identity"
    )


@pytest.mark.parametrize("component", _COMPONENTS, ids=list(_COMPONENTS))
def test_missing_required_component_fails_closed(component: str) -> None:
    compute = _identity()
    broken = _policy()
    del broken[component]
    with pytest.raises(ValueError) as excinfo:
        compute(broken)
    assert component in str(excinfo.value), (
        f"{_MISSING}: the ValueError must name the missing component {component!r}"
    )


@pytest.mark.parametrize("component", _COMPONENTS, ids=list(_COMPONENTS))
def test_none_required_component_fails_closed(component: str) -> None:
    compute = _identity()
    broken = _policy()
    broken[component] = None
    with pytest.raises(ValueError) as excinfo:
        compute(broken)
    assert component in str(excinfo.value), (
        f"{_MISSING}: the ValueError must name the None component {component!r}"
    )


@pytest.mark.parametrize("component", _COMPONENTS, ids=list(_COMPONENTS))
def test_blank_required_component_fails_closed(component: str) -> None:
    compute = _identity()
    broken = _policy()
    broken[component] = "   "
    with pytest.raises(ValueError) as excinfo:
        compute(broken)
    assert component in str(excinfo.value), (
        f"{_MISSING}: the ValueError must name the blank component {component!r}"
    )


def test_continuation_reports_no_violation_for_pinned_identity() -> None:
    guard = _continuation()
    pinned = _policy()
    successor = dict(pinned)
    violations = tuple(guard({"pinned": pinned, "successor": successor}))
    assert violations == (), (
        f"{_MISSING}: a successor carrying the pinned identity must report no violations"
    )


@pytest.mark.parametrize("component", _COMPONENTS, ids=list(_COMPONENTS))
def test_continuation_flags_changed_component(component: str) -> None:
    guard = _continuation()
    pinned = _policy()
    successor = _policy()
    successor[component] = f"weakened:{component}"
    violations = tuple(guard({"pinned": pinned, "successor": successor}))
    assert violations == (_CHANGED,), (
        f"{_MISSING}: a successor changing {component!r} must be flagged {_CHANGED!r}"
    )
