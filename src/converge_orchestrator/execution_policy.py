"""INV-ENV-001: pure, offline environment-metadata privilege-expansion guard (fails closed).

Repository-declared toolchain or execution-environment metadata SHALL NOT expand controller
privileges, secret authority, host access, network authority, or sandbox policy beyond the
run's pinned execution policy (SOURCE_OF_TRUTH L179 [4. Global invariants]). Unknown keys are
ignored and the result is deterministic and independent of the input mapping's key order.
"""

from collections.abc import Mapping

CONTROLLER_PRIVILEGES = "controller_privileges_expanded"
SECRET_AUTHORITY = "secret_authority_expanded"
HOST_ACCESS = "host_access_expanded"
NETWORK_AUTHORITY = "network_authority_expanded"
SANDBOX_POLICY = "sandbox_policy_expanded"

_DIMENSIONS = (
    (CONTROLLER_PRIVILEGES, "requested_controller_privileges", "pinned_controller_privileges"),
    (SECRET_AUTHORITY, "requested_secret_authority", "pinned_secret_authority"),
    (HOST_ACCESS, "requested_host_access", "pinned_host_access"),
    (NETWORK_AUTHORITY, "requested_network_authority", "pinned_network_authority"),
    (SANDBOX_POLICY, "requested_sandbox_policy", "pinned_sandbox_policy"),
)


def _is_expanded(requested: object, pinned: object) -> bool:
    """Fail closed: an absent or non-set pin is expanded; an absent request fits any pin."""
    if pinned is None or not isinstance(pinned, (set, frozenset)):
        return True
    if requested is None:
        return False
    if not isinstance(requested, (set, frozenset)):
        return True
    return not requested.issubset(pinned)


def evaluate_environment_privilege_expansion(state: Mapping[str, object]) -> tuple[str, ...]:
    """Return the expanded INV-ENV-001 clauses in fixed order; non-empty means must not honor."""
    if not isinstance(state, Mapping):
        raise TypeError("environment privilege-expansion state must be a mapping of grants")
    return tuple(
        clause for clause, requested_key, pinned_key in _DIMENSIONS
        if _is_expanded(state.get(requested_key), state.get(pinned_key))
    )
