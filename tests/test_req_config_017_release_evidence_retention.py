"""REQ-CONFIG-017: release-evidence retention and archival retention are declared.

REQ-CONFIG-017 (CONVERGE_ORCHESTRATOR_2_0_SOURCE_OF_TRUTH.md:L215
[6. Project configuration and secret sources]): the project configuration SHALL
define a finite release-evidence retention horizon or an explicitly durable archival
policy, and SHALL define which external evidence classes and released
repository-object archives require durable capture, immutable retention proof, or
both before they can contribute to release convergence.

These offline, deterministic tests pin that contract against the pure, fail-closed
guard ``converge_orchestrator.config.evaluate_release_evidence_retention`` over a
project-configuration mapping carrying a ``release_evidence_retention`` section:

* the guard returns an empty tuple for a complete policy declaring a finite
  positive retention horizon, with every external evidence class and released
  repository-object archive requiring durable capture, immutable retention proof,
  or both;
* the guard returns an empty tuple for a complete policy declaring an explicit
  durable archival policy in place of a horizon;
* requiring durable capture alone, or immutable retention proof alone, satisfies the
  "durable capture, immutable retention proof, or both" contract;
* the guard reports ``retention_mode_undeclared`` when the retention mode is absent,
  blank, or unknown;
* the guard reports ``retention_horizon_invalid`` when the mode is a finite horizon
  but the horizon is absent, zero, negative, non-numeric, or non-finite;
* the guard reports ``durable_archival_policy_undeclared`` when the mode is a durable
  archival policy but the policy is absent, blank, or non-string;
* the guard reports ``external_evidence_classes_undeclared`` when the external
  evidence classes are absent or empty, and
  ``external_evidence_class_retention_undeclared:<name>`` when a class is malformed
  or requires neither durable capture nor immutable retention proof;
* the guard reports ``released_repository_object_archives_undeclared`` when the
  released repository-object archives are absent or empty, and
  ``released_repository_object_archive_retention_undeclared:<name>`` when an archive
  is malformed or requires neither durable capture nor immutable retention proof;
* the guard reports clauses in the fixed order: retention mode, external evidence
  classes, released repository-object archives.

The guard does not exist yet, so every evaluation below fails with the literal
TDD RED evidence marker ``REQ-CONFIG-017-RELEASE-EVIDENCE-RETENTION-GUARD-MISSING``
instead of silently accepting a configuration whose release-evidence retention or
archival retention is under-declared. No network, model, provider, subprocess, git,
or filesystem access occurs.
"""

from __future__ import annotations

from typing import Any

import pytest

# TDD RED evidence marker (tdd.expected_failure_pattern): carried by every failure
# below until the pure guard converge_orchestrator.config.evaluate_release_evidence_retention
# is added. It is a literal, unique string that does not appear anywhere else in the
# repository, so its appearance in a test run is deterministic RED evidence.
_GUARD_MISSING = "REQ-CONFIG-017-RELEASE-EVIDENCE-RETENTION-GUARD-MISSING"

_SECTION = "release_evidence_retention"
_MODE_FINITE = "finite_horizon"
_MODE_DURABLE = "durable_archival"


def _guard():
    """Return the guard, failing closed with the literal RED marker when it is absent."""
    try:
        from converge_orchestrator.config import evaluate_release_evidence_retention
    except ImportError as exc:
        raise AssertionError(_GUARD_MISSING) from exc
    return evaluate_release_evidence_retention


def _evaluate(policy: dict[str, Any]) -> tuple[str, ...]:
    """Evaluate release-evidence retention for ``policy`` through the guard."""
    return _guard()(policy)


def _requirement(
    durable_capture: bool = True,
    immutable_retention_proof: bool = True,
) -> dict[str, Any]:
    """One durable-capture / immutable-retention-proof requirement declaration."""
    return {
        "durable_capture": durable_capture,
        "immutable_retention_proof": immutable_retention_proof,
    }


def _complete_section(mode: str = _MODE_FINITE) -> dict[str, Any]:
    """A complete ``release_evidence_retention`` section satisfying REQ-CONFIG-017."""
    section: dict[str, Any] = {
        "retention_mode": mode,
        "external_evidence_classes": {
            "ci_logs": _requirement(),
            "artifact_store": _requirement(),
        },
        "released_repository_object_archives": {
            "git_object_store": _requirement(),
        },
    }
    if mode == _MODE_FINITE:
        section["retention_horizon"] = 31536000
    else:
        section["durable_archival_policy"] = "durable-object-store-archive"
    return section


def _complete_policy(mode: str = _MODE_FINITE) -> dict[str, Any]:
    """A complete project-configuration mapping satisfying REQ-CONFIG-017."""
    return {_SECTION: _complete_section(mode)}


def test_complete_finite_horizon_policy_is_accepted() -> None:
    # A finite positive retention horizon plus complete evidence-class and
    # repository-object-archive requirements: the guard is satisfied and reports no
    # clause.
    assert _evaluate(_complete_policy(_MODE_FINITE)) == ()


def test_complete_durable_archival_policy_is_accepted() -> None:
    # An explicit durable archival policy declared in place of a horizon, plus
    # complete evidence-class and repository-object-archive requirements: the guard
    # is satisfied and reports no clause.
    assert _evaluate(_complete_policy(_MODE_DURABLE)) == ()


def test_either_requirement_alone_is_sufficient() -> None:
    # Requiring durable capture alone, or immutable retention proof alone, satisfies
    # the "durable capture, immutable retention proof, or both" contract, so a
    # class/archive declaring only one is accepted with no clause.
    policy = _complete_policy()
    section = policy[_SECTION]
    section["external_evidence_classes"]["ci_logs"] = _requirement(True, False)
    section["released_repository_object_archives"]["git_object_store"] = _requirement(
        False, True
    )
    assert _evaluate(policy) == ()


def test_missing_retention_mode_fails_closed() -> None:
    # No retention mode is declared, so neither a horizon nor an archival policy can
    # be verified: the guard must fail closed naming the retention mode.
    policy = _complete_policy()
    del policy[_SECTION]["retention_mode"]
    assert _evaluate(policy) == ("retention_mode_undeclared",)


def test_unknown_retention_mode_fails_closed() -> None:
    # A retention mode outside the two declared modes is not a finite horizon or an
    # explicit durable archival policy: the guard must fail closed naming the mode.
    policy = _complete_policy()
    policy[_SECTION]["retention_mode"] = "indefinite"
    assert _evaluate(policy) == ("retention_mode_undeclared",)


def test_absent_retention_horizon_fails_closed() -> None:
    # The mode is a finite horizon but no horizon is declared: the guard must fail
    # closed naming the retention horizon rather than accepting an open-ended one.
    policy = _complete_policy(_MODE_FINITE)
    del policy[_SECTION]["retention_horizon"]
    assert _evaluate(policy) == ("retention_horizon_invalid",)


@pytest.mark.parametrize(
    "horizon",
    [0, -5, "forever", float("inf")],
    ids=["zero", "negative", "non-numeric", "non-finite"],
)
def test_invalid_retention_horizon_fails_closed(horizon: Any) -> None:
    # The mode is a finite horizon but the horizon is zero, negative, non-numeric, or
    # non-finite, so it is not a finite positive horizon: the guard must fail closed
    # naming the retention horizon.
    policy = _complete_policy(_MODE_FINITE)
    policy[_SECTION]["retention_horizon"] = horizon
    assert _evaluate(policy) == ("retention_horizon_invalid",)


def test_absent_durable_archival_policy_fails_closed() -> None:
    # The mode is a durable archival policy but no policy is declared: the guard must
    # fail closed naming the durable archival policy.
    policy = _complete_policy(_MODE_DURABLE)
    del policy[_SECTION]["durable_archival_policy"]
    assert _evaluate(policy) == ("durable_archival_policy_undeclared",)


@pytest.mark.parametrize(
    "value",
    ["", "   ", 123],
    ids=["empty", "blank", "non-string"],
)
def test_invalid_durable_archival_policy_fails_closed(value: Any) -> None:
    # The mode is a durable archival policy but the policy is blank or non-string, so
    # it is not an explicit durable archival policy: the guard must fail closed naming
    # the durable archival policy.
    policy = _complete_policy(_MODE_DURABLE)
    policy[_SECTION]["durable_archival_policy"] = value
    assert _evaluate(policy) == ("durable_archival_policy_undeclared",)


def test_absent_external_evidence_classes_fails_closed() -> None:
    # No external evidence classes are declared, so the configuration cannot state
    # which require durable capture or immutable retention proof: the guard must fail
    # closed naming the scope.
    policy = _complete_policy()
    del policy[_SECTION]["external_evidence_classes"]
    assert _evaluate(policy) == ("external_evidence_classes_undeclared",)


def test_empty_external_evidence_classes_fails_closed() -> None:
    # The external evidence classes are declared empty, so no class's retention
    # requirement can be verified: the guard must fail closed naming the scope.
    policy = _complete_policy()
    policy[_SECTION]["external_evidence_classes"] = {}
    assert _evaluate(policy) == ("external_evidence_classes_undeclared",)


def test_absent_released_repository_object_archives_fails_closed() -> None:
    # No released repository-object archives are declared, so the configuration cannot
    # state which require durable capture or immutable retention proof: the guard must
    # fail closed naming the scope.
    policy = _complete_policy()
    del policy[_SECTION]["released_repository_object_archives"]
    assert _evaluate(policy) == ("released_repository_object_archives_undeclared",)


def test_empty_released_repository_object_archives_fails_closed() -> None:
    # The released repository-object archives are declared empty, so no archive's
    # retention requirement can be verified: the guard must fail closed naming the
    # scope.
    policy = _complete_policy()
    policy[_SECTION]["released_repository_object_archives"] = {}
    assert _evaluate(policy) == ("released_repository_object_archives_undeclared",)


def test_evidence_class_requiring_neither_fails_closed() -> None:
    # One external evidence class requires neither durable capture nor immutable
    # retention proof, so it is under-declared: the guard must fail closed naming that
    # class.
    policy = _complete_policy()
    policy[_SECTION]["external_evidence_classes"]["ci_logs"] = _requirement(False, False)
    assert _evaluate(policy) == (
        "external_evidence_class_retention_undeclared:ci_logs",
    )


def test_archive_requiring_neither_fails_closed() -> None:
    # One released repository-object archive requires neither durable capture nor
    # immutable retention proof, so it is under-declared: the guard must fail closed
    # naming that archive.
    policy = _complete_policy()
    policy[_SECTION]["released_repository_object_archives"][
        "git_object_store"
    ] = _requirement(False, False)
    assert _evaluate(policy) == (
        "released_repository_object_archive_retention_undeclared:git_object_store",
    )


def test_malformed_evidence_class_declaration_fails_closed() -> None:
    # One external evidence class's declaration is a non-mapping, so its durable
    # capture and immutable retention proof cannot be read: the guard must fail closed
    # naming that class.
    policy = _complete_policy()
    policy[_SECTION]["external_evidence_classes"]["ci_logs"] = "durable"
    assert _evaluate(policy) == (
        "external_evidence_class_retention_undeclared:ci_logs",
    )


def test_violations_reported_in_fixed_order() -> None:
    # A retention-mode gap, an under-declared evidence class, and an under-declared
    # archive all hold at once: the guard must report the clauses in the fixed order
    # (retention mode, external evidence classes, released repository-object
    # archives), naming the offending items.
    policy = _complete_policy(_MODE_FINITE)
    section = policy[_SECTION]
    del section["retention_mode"]
    section["external_evidence_classes"]["ci_logs"] = _requirement(False, False)
    section["released_repository_object_archives"]["git_object_store"] = _requirement(
        False, False
    )
    assert _evaluate(policy) == (
        "retention_mode_undeclared",
        "external_evidence_class_retention_undeclared:ci_logs",
        "released_repository_object_archive_retention_undeclared:git_object_store",
    )
