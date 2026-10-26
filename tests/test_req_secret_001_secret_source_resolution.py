"""REQ-SECRET-001: first-class external secrets-file resolution without role-semantic drift.

REQ-SECRET-001 (CONVERGE_ORCHESTRATOR_2_0_SOURCE_OF_TRUTH.md:L221
[6. Project configuration and secret sources]): project configuration SHALL
support a first-class external secrets-file path and SHALL also permit
deployment-environment or configured credential-backend resolution without
changing role semantics.

These offline, deterministic tests pin that contract against the pure,
fail-closed guard ``converge_orchestrator.config.evaluate_secret_source_resolution``
over a project-configuration mapping carrying:

* ``role_semantics`` -- the base role semantics, a mapping of role name to a
  non-empty string semantics description; every value must be a string; and
* ``secret_resolution`` -- the secret-resolution mapping carrying:
  * ``secrets_file`` -- the first-class external secrets-file path, which must be
    a non-blank absolute path; and
  * optional ``deployment_environment`` and ``credential_backend`` alternate
    resolution declarations, each carrying a ``role_semantics`` mapping that must
    be identical to the base ``role_semantics``.

The guard accepts (returns an empty tuple) a policy whose ``secret_resolution``
declares a non-blank absolute ``secrets_file`` and whose alternate resolution
declarations, when present, carry role semantics identical to the base
``role_semantics`` -- alone and with a ``deployment_environment`` or
``credential_backend`` resolution source.

The guard reports the fixed clause ``external_secrets_file_not_declared`` when
the ``secrets_file`` is absent, null, blank, or non-string, and the fixed clause
``external_secrets_file_not_absolute`` when it is a relative path.

The guard reports the fixed clauses
``secret_resolution_alters_role_semantics:deployment_environment`` and
``secret_resolution_alters_role_semantics:credential_backend`` (in that order)
when an alternate resolution declaration changes or adds role semantics relative
to the base ``role_semantics``.

It fails closed with ``ValueError`` for a non-mapping policy, a missing or
non-mapping ``secret_resolution``, or a missing or non-mapping ``role_semantics``
whose values are not all strings.

The guard does not include, echo, persist, or compare secret values; it assesses
only the secrets-file declaration and the role-semantics identities. No
filesystem, environment, network, model, git, or subprocess access occurs.

The guard does not exist yet, so every evaluation below fails with the literal
TDD RED evidence marker ``REQ-SECRET-001-SECRET-SOURCE-RESOLUTION-GUARD-MISSING``
instead of silently accepting a policy that omits the first-class external
secrets-file or lets an alternate resolution source alter role semantics.
"""

from __future__ import annotations

from typing import Any

import pytest

# TDD RED evidence marker (tdd.expected_failure_pattern): carried by every failure
# below until the pure guard converge_orchestrator.config.evaluate_secret_source_resolution
# is added. It is a literal, unique string that does not appear anywhere else in the
# repository, so its appearance in a test run is deterministic RED evidence.
_GUARD_MISSING = "REQ-SECRET-001-SECRET-SOURCE-RESOLUTION-GUARD-MISSING"

_MISSING = object()

# A non-blank absolute path (POSIX spelling; platform-independent under the
# guard's offline path-shape classification).
_ABSOLUTE_SECRETS_FILE = "/secrets/external"
# A relative spelling that must be reported as not absolute.
_RELATIVE_SECRETS_FILE = "secrets/external"

# The base role semantics: a mapping of role name to a string semantics
# description. Alternate resolution sources must carry an identical mapping.
_BASE_ROLE_SEMANTICS = {
    "orchestrator": "controls the workflow; never writes repository source",
    "builder": "writes candidate source only inside its assigned worktree",
}

# An alternate source that ADDS a role relative to the base (a change).
_ADDED_ROLE_SEMANTICS = {
    **_BASE_ROLE_SEMANTICS,
    "reviewer": "reviews the candidate without writing repository source",
}

# An alternate source that CHANGES a role's semantics relative to the base.
_CHANGED_ROLE_SEMANTICS = {
    **_BASE_ROLE_SEMANTICS,
    "builder": "writes candidate source anywhere in the repository",
}


def _guard():
    """Return the guard, failing closed with the literal RED marker when absent."""
    try:
        from converge_orchestrator.config import evaluate_secret_source_resolution
    except ImportError as exc:
        raise AssertionError(_GUARD_MISSING) from exc
    return evaluate_secret_source_resolution


def _policy(**overrides: Any) -> dict[str, Any]:
    """A policy with a base role semantics and an absolute, first-class secrets-file."""
    policy: dict[str, Any] = {
        "role_semantics": dict(_BASE_ROLE_SEMANTICS),
        "secret_resolution": {
            "secrets_file": _ABSOLUTE_SECRETS_FILE,
        },
    }
    policy.update(overrides)
    return policy


def _set_secrets_file(policy: dict[str, Any], secrets_file: Any) -> None:
    """Assign ``secrets_file`` on the policy, deleting the key when ``_MISSING``."""
    secret_resolution = policy["secret_resolution"]
    if secrets_file is _MISSING:
        del secret_resolution["secrets_file"]
    else:
        secret_resolution["secrets_file"] = secrets_file


def _declare(policy: dict[str, Any], source: str, role_semantics: Any) -> None:
    """Declare one alternate resolution ``source`` (deployment_environment or
    credential_backend) carrying a ``role_semantics`` mapping."""
    policy["secret_resolution"][source] = {"role_semantics": role_semantics}


def test_absolute_secrets_file_alone_is_accepted() -> None:
    # The first-class external secrets-file path is a non-blank absolute path and
    # no alternate resolution source is declared: the guard is satisfied and
    # reports no clause.
    assert _guard()(_policy()) == ()


def test_absolute_secrets_file_with_matching_deployment_environment_is_accepted() -> None:
    # A deployment-environment alternate resolution source whose role semantics
    # are identical to the base does not change role semantics: the guard is
    # satisfied and reports no clause.
    policy = _policy()
    _declare(policy, "deployment_environment", dict(_BASE_ROLE_SEMANTICS))
    assert _guard()(policy) == ()


def test_absolute_secrets_file_with_matching_credential_backend_is_accepted() -> None:
    # A credential-backend alternate resolution source whose role semantics are
    # identical to the base does not change role semantics: the guard is
    # satisfied and reports no clause.
    policy = _policy()
    _declare(policy, "credential_backend", dict(_BASE_ROLE_SEMANTICS))
    assert _guard()(policy) == ()


def test_absolute_secrets_file_with_both_matching_alternates_is_accepted() -> None:
    # Both alternate resolution sources carry role semantics identical to the
    # base and are accepted together with the first-class secrets-file.
    policy = _policy()
    _declare(policy, "deployment_environment", dict(_BASE_ROLE_SEMANTICS))
    _declare(policy, "credential_backend", dict(_BASE_ROLE_SEMANTICS))
    assert _guard()(policy) == ()


@pytest.mark.parametrize(
    "secrets_file",
    [_MISSING, None, "", "   ", 123, []],
    ids=["missing", "null", "empty", "blank", "non-string-number", "non-string-list"],
)
def test_not_declared_secrets_file_reports_clause(secrets_file: Any) -> None:
    # An absent, null, blank, or non-string secrets-file is not a usable
    # first-class external secrets-file path: the guard reports the
    # not-declared clause and no other.
    policy = _policy()
    _set_secrets_file(policy, secrets_file)
    assert _guard()(policy) == ("external_secrets_file_not_declared",)


def test_relative_secrets_file_reports_not_absolute() -> None:
    # A relative secrets-file path is not a first-class absolute external
    # secrets-file path: the guard reports the not-absolute clause and no other.
    policy = _policy()
    _set_secrets_file(policy, _RELATIVE_SECRETS_FILE)
    assert _guard()(policy) == ("external_secrets_file_not_absolute",)


def test_deployment_environment_adding_a_role_is_rejected() -> None:
    # A deployment-environment alternate that ADDS a role relative to the base
    # changes role semantics: the guard reports the deployment-environment
    # alteration clause and no other.
    policy = _policy()
    _declare(policy, "deployment_environment", dict(_ADDED_ROLE_SEMANTICS))
    assert _guard()(policy) == ("secret_resolution_alters_role_semantics:deployment_environment",)


def test_credential_backend_changing_a_role_is_rejected() -> None:
    # A credential-backend alternate that CHANGES a role's semantics relative to
    # the base alters role semantics: the guard reports the credential-backend
    # alteration clause and no other.
    policy = _policy()
    _declare(policy, "credential_backend", dict(_CHANGED_ROLE_SEMANTICS))
    assert _guard()(policy) == ("secret_resolution_alters_role_semantics:credential_backend",)


def test_both_alternates_altering_roles_report_in_fixed_order() -> None:
    # Both alternate resolution sources alter role semantics relative to the
    # base: the guard reports the deployment-environment clause before the
    # credential-backend clause, in that fixed order.
    policy = _policy()
    _declare(policy, "deployment_environment", dict(_ADDED_ROLE_SEMANTICS))
    _declare(policy, "credential_backend", dict(_CHANGED_ROLE_SEMANTICS))
    assert _guard()(policy) == (
        "secret_resolution_alters_role_semantics:deployment_environment",
        "secret_resolution_alters_role_semantics:credential_backend",
    )


@pytest.mark.parametrize(
    "policy",
    ["converge", 123, [], None],
    ids=["string", "number", "list", "null"],
)
def test_non_mapping_policy_fails_closed(policy: Any) -> None:
    # A policy that is not a mapping cannot be assessed at all: the guard fails
    # closed with ValueError rather than silently passing.
    with pytest.raises(ValueError):
        _guard()(policy)


def test_missing_secret_resolution_fails_closed() -> None:
    # Without a secret_resolution mapping there is no first-class external
    # secrets-file to assess: the guard fails closed with ValueError.
    with pytest.raises(ValueError):
        _guard()({"role_semantics": dict(_BASE_ROLE_SEMANTICS)})


@pytest.mark.parametrize(
    "secret_resolution",
    ["converge", 123, None],
    ids=["string", "number", "null"],
)
def test_non_mapping_secret_resolution_fails_closed(secret_resolution: Any) -> None:
    # A secret_resolution that is not a mapping cannot declare a first-class
    # external secrets-file: the guard fails closed with ValueError.
    with pytest.raises(ValueError):
        _guard()(
            {
                "role_semantics": dict(_BASE_ROLE_SEMANTICS),
                "secret_resolution": secret_resolution,
            }
        )


def test_missing_role_semantics_fails_closed() -> None:
    # Without a base role_semantics mapping there is no baseline to compare an
    # alternate resolution source against: the guard fails closed with
    # ValueError.
    with pytest.raises(ValueError):
        _guard()({"secret_resolution": {"secrets_file": _ABSOLUTE_SECRETS_FILE}})


@pytest.mark.parametrize(
    "role_semantics",
    ["orchestrator", 123, []],
    ids=["string", "number", "list"],
)
def test_non_mapping_role_semantics_fails_closed(role_semantics: Any) -> None:
    # A base role_semantics that is not a mapping cannot be compared against an
    # alternate source's semantics: the guard fails closed with ValueError.
    with pytest.raises(ValueError):
        _guard()(
            {
                "role_semantics": role_semantics,
                "secret_resolution": {"secrets_file": _ABSOLUTE_SECRETS_FILE},
            }
        )


def test_non_string_role_semantics_entry_fails_closed() -> None:
    # A base role_semantics entry whose value is not a string is a malformed
    # semantics identity: the guard fails closed with ValueError rather than
    # comparing an ill-formed role semantics against an alternate source.
    with pytest.raises(ValueError):
        _guard()(
            {
                "role_semantics": {"orchestrator": 123},
                "secret_resolution": {"secrets_file": _ABSOLUTE_SECRETS_FILE},
            }
        )
