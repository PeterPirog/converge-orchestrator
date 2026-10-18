"""RED lock test for INV-ENV-001: environment-metadata privilege-expansion guard.

INV-ENV-001 (CONVERGE_ORCHESTRATOR_2_0_SOURCE_OF_TRUTH.md:L179 [4. Global invariants]):
Toolchain or execution-environment metadata declared by the repository SHALL NOT expand
controller privileges, secret authority, host access, network authority, or sandbox policy
beyond the run's pinned execution policy.

The codebase has no guard comparing repository-declared environment metadata against the run's
pinned execution policy. These deterministic offline tests therefore lock the not-yet-existing
pure primitive ``converge_orchestrator.execution_policy.evaluate_environment_privilege_expansion``:

- against the old implementation the module/symbol does not exist, so every test fails with the
  literal marker ``ENV001_ENVIRONMENT_PRIVILEGE_EXPANSION_MISSING``;
- once the primitive is added, the same tests must pass. It takes a single plain mapping
  describing, per protected dimension, the grant the repository-declared toolchain or
  execution-environment metadata requests (``requested_*`` fields) and the run's pinned
  execution-policy grant (``pinned_*`` fields), and returns the expanded dimensions in a fixed
  order:

  * ``"controller_privileges_expanded"`` -- controller privileges requested beyond the pin;
  * ``"secret_authority_expanded"``      -- secret authority requested beyond the pin;
  * ``"host_access_expanded"``           -- host access requested beyond the pin;
  * ``"network_authority_expanded"``     -- network authority requested beyond the pin;
  * ``"sandbox_policy_expanded"``        -- sandbox policy requested beyond the pin.

A dimension is expanded when its requested grant is present and contains a capability the
pinned grant does not. The guard fails closed: a dimension whose pinned grant is absent
(``None``) is always expanded, because the pin cannot prove the request stays within it. An
absent requested grant is within any present pin. Grants are plain sets of capability tokens
(``frozenset``), so the result is deterministic and independent of input mapping key order.
No network, model, provider, subprocess, git, or filesystem side effects in the tested path.
"""

from __future__ import annotations

import importlib
from collections.abc import Callable, Iterable, Mapping
from typing import cast

_MISSING = "ENV001_ENVIRONMENT_PRIVILEGE_EXPANSION_MISSING"
_MODULE = "converge_orchestrator.execution_policy"
_NAME = "evaluate_environment_privilege_expansion"

_CLAUSE_CONTROLLER_PRIVILEGES = "controller_privileges_expanded"
_CLAUSE_SECRET_AUTHORITY = "secret_authority_expanded"
_CLAUSE_HOST_ACCESS = "host_access_expanded"
_CLAUSE_NETWORK_AUTHORITY = "network_authority_expanded"
_CLAUSE_SANDBOX_POLICY = "sandbox_policy_expanded"

# A pure guard: one plain mapping of requested/pinned grants -> the (possibly empty) iterable
# of expanded-dimension clauses, in the fixed order controller, secret, host, network, sandbox.
Guard = Callable[[Mapping[str, object]], Iterable[str]]


def _guard() -> Guard:
    """Resolve the INV-ENV-001 primitive, failing with the RED marker if it is absent.

    Every test routes through this helper first, so against the old tree (where
    ``converge_orchestrator.execution_policy`` does not exist) each test fails with the
    literal ``ENV001_ENVIRONMENT_PRIVILEGE_EXPANSION_MISSING`` marker rather than an opaque
    import error.
    """
    try:
        module = importlib.import_module(_MODULE)
    except ModuleNotFoundError as exc:
        raise AssertionError(
            f"{_MISSING}: {_MODULE}.{_NAME} is not defined; "
            "INV-ENV-001 requires a deterministic, offline guard that flags every protected "
            "execution-policy dimension in which repository-declared toolchain or "
            "execution-environment metadata requests capabilities beyond the run's pinned "
            "execution policy, failing closed when the pinned grant cannot be proven."
        ) from exc
    guard = getattr(module, _NAME, None)
    assert callable(guard), (
        f"{_MISSING}: {_MODULE}.{_NAME} is not defined; "
        "INV-ENV-001 requires a deterministic environment-metadata privilege-expansion guard."
    )
    return cast(Guard, guard)


def _state(
    *,
    requested_controller: object = frozenset({"exec"}),
    requested_secret: object = frozenset(),
    requested_host: object = frozenset(),
    requested_network: object = frozenset(),
    requested_sandbox: object = frozenset(),
    pinned_controller: object = frozenset({"exec"}),
    pinned_secret: object = frozenset({"read"}),
    pinned_host: object = frozenset({"worktree"}),
    pinned_network: object = frozenset({"localhost"}),
    pinned_sandbox: object = frozenset({"standard"}),
) -> dict[str, object]:
    """Build a plain grant mapping with no coupling to any concrete run or repository state."""
    return {
        "requested_controller_privileges": requested_controller,
        "requested_secret_authority": requested_secret,
        "requested_host_access": requested_host,
        "requested_network_authority": requested_network,
        "requested_sandbox_policy": requested_sandbox,
        "pinned_controller_privileges": pinned_controller,
        "pinned_secret_authority": pinned_secret,
        "pinned_host_access": pinned_host,
        "pinned_network_authority": pinned_network,
        "pinned_sandbox_policy": pinned_sandbox,
    }


def _reversed_keys(state: Mapping[str, object]) -> dict[str, object]:
    """Rebuild ``state`` with the same items in the opposite mapping key order."""
    return {key: state[key] for key in reversed(list(state))}


def test_grants_within_pin_report_no_expansion() -> None:
    guard = _guard()
    # Every requested grant is contained in its pinned grant (equal or a proper subset), so no
    # protected dimension is expanded beyond the run's pinned execution policy.
    state = _state(
        requested_secret=frozenset({"read"}),
        pinned_secret=frozenset({"read", "write"}),
        requested_network=frozenset({"localhost"}),
    )
    assert tuple(guard(state)) == ()


def test_absent_requests_report_no_expansion() -> None:
    guard = _guard()
    # An absent (None) requested grant requests no capabilities, so it is within every present
    # pinned grant; only the pinned side is fail-closed.
    state = _state(
        requested_controller=None,
        requested_secret=None,
        requested_host=None,
        requested_network=None,
        requested_sandbox=None,
    )
    assert tuple(guard(state)) == ()


def test_controller_privileges_beyond_pin_flagged() -> None:
    guard = _guard()
    # The repository-declared metadata requests a controller privilege the pin does not grant.
    state = _state(requested_controller=frozenset({"exec", "admin"}))
    assert tuple(guard(state)) == (_CLAUSE_CONTROLLER_PRIVILEGES,)


def test_secret_authority_beyond_pin_flagged() -> None:
    guard = _guard()
    # The metadata requests a secret authority token the pin does not grant.
    state = _state(requested_secret=frozenset({"read", "write"}))
    assert tuple(guard(state)) == (_CLAUSE_SECRET_AUTHORITY,)


def test_host_access_beyond_pin_flagged() -> None:
    guard = _guard()
    # The metadata requests a host-access scope the pin does not grant.
    state = _state(requested_host=frozenset({"worktree", "raw"}))
    assert tuple(guard(state)) == (_CLAUSE_HOST_ACCESS,)


def test_network_authority_beyond_pin_flagged() -> None:
    guard = _guard()
    # The metadata requests a network scope (egress) the pin does not grant.
    state = _state(requested_network=frozenset({"localhost", "egress"}))
    assert tuple(guard(state)) == (_CLAUSE_NETWORK_AUTHORITY,)


def test_sandbox_policy_beyond_pin_flagged() -> None:
    guard = _guard()
    # The metadata requests a sandbox policy the pin does not grant.
    state = _state(requested_sandbox=frozenset({"standard", "privileged"}))
    assert tuple(guard(state)) == (_CLAUSE_SANDBOX_POLICY,)


def test_absent_pinned_grant_fails_closed() -> None:
    guard = _guard()
    # The pinned network grant is absent, so the pin cannot prove the requested network scope is
    # within it; the guard fails closed even though the metadata requests no network scope.
    state = _state(pinned_network=None)
    assert tuple(guard(state)) == (_CLAUSE_NETWORK_AUTHORITY,)


def test_all_expansions_reported_in_fixed_order() -> None:
    guard = _guard()
    # Every protected dimension requests a capability its pinned grant lacks; the clauses are
    # reported in the fixed order: controller, secret, host, network, sandbox.
    state = _state(
        requested_controller=frozenset({"exec", "admin"}),
        requested_secret=frozenset({"read", "write"}),
        requested_host=frozenset({"worktree", "raw"}),
        requested_network=frozenset({"localhost", "egress"}),
        requested_sandbox=frozenset({"standard", "privileged"}),
    )
    assert tuple(guard(state)) == (
        _CLAUSE_CONTROLLER_PRIVILEGES,
        _CLAUSE_SECRET_AUTHORITY,
        _CLAUSE_HOST_ACCESS,
        _CLAUSE_NETWORK_AUTHORITY,
        _CLAUSE_SANDBOX_POLICY,
    )


def test_result_independent_of_mapping_key_order() -> None:
    guard = _guard()
    # The same requested/pinned grants in the opposite mapping key order must yield the same
    # expanded-dimension clauses in the same fixed order.
    state = _state(
        requested_controller=frozenset({"exec", "admin"}),
        requested_host=frozenset({"worktree", "raw"}),
    )
    expected = (_CLAUSE_CONTROLLER_PRIVILEGES, _CLAUSE_HOST_ACCESS)
    assert tuple(guard(state)) == expected
    assert tuple(guard(_reversed_keys(state))) == expected
