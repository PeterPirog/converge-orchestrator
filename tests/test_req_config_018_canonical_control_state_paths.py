"""REQ-CONFIG-018: durable control-state paths resolve to one canonical identity.

REQ-CONFIG-018 (CONVERGE_ORCHESTRATOR_2_0_SOURCE_OF_TRUTH.md:L216
[6. Project configuration and secret sources]): every filesystem path that
selects durable control-state authority SHALL resolve to one canonical absolute
identity from pinned project configuration or another explicitly pinned
authority source before project registration or run start, and SHALL NOT depend
on process current working directory.

These offline, deterministic tests pin that contract against the pure,
fail-closed guard ``converge_orchestrator.config.evaluate_canonical_durable_control_state_paths``
over a durable control-state path declaration mapping carrying:

* ``durable_control_state_path`` -- the path declaration under evaluation;
* ``pinned_authority_source`` -- the explicitly pinned authority source
  carrying a ``kind`` (``pinned_project_config`` or
  ``pinned_authority_source``) and an absolute ``base`` directory that anchors
  relative declarations; and
* ``process_cwd`` -- the process current working directory.

The guard accepts (returns an empty tuple) an absolute declaration, which is
already a canonical absolute identity, and a relative declaration anchored to a
pinned base whose canonical identity differs from the process CWD, which
resolves to one canonical absolute identity without CWD dependence.

The guard reports the fixed clause ``pinned_authority_source_undeclared`` for a
relative declaration whose authority source is missing, unpinned, blank-kind,
or non-absolute-base, and the fixed clause
``control_state_path_depends_on_process_cwd`` when a relative declaration is
anchored on the process CWD (including a canonical re-spelling of the same
identity). A declaration triggers at most one of the two clauses.

It fails closed with ``ValueError`` for a missing or empty path declaration, a
malformed path value, and a missing or non-absolute ``process_cwd``.

The guard does not exist yet, so every evaluation below fails with the literal
TDD RED evidence marker ``REQ-CONFIG-018-CANONICAL-CONTROL-STATE-PATH-GUARD-MISSING``
instead of silently accepting a control-state path whose authority depends on
the process working directory or on an unpinned source. No filesystem, network,
model, git, or subprocess access occurs.
"""

from __future__ import annotations

from typing import Any

import pytest

# TDD RED evidence marker (tdd.expected_failure_pattern): carried by every
# failure below until the pure guard
# converge_orchestrator.config.evaluate_canonical_durable_control_state_paths
# is added. It is a literal, unique string that does not appear anywhere else
# in the repository, so its appearance in a test run is deterministic RED
# evidence.
_GUARD_MISSING = "REQ-CONFIG-018-CANONICAL-CONTROL-STATE-PATH-GUARD-MISSING"

_MISSING = object()

_PINNED_BASE = "/projects/pinned/converge"
_PROCESS_CWD = "/work/runtime/cwd"
_ABSOLUTE_PATH = f"{_PINNED_BASE}/state/control.sqlite"
_RELATIVE_PATH = "state/control.sqlite"


def _guard():
    """Return the guard, failing closed with the literal RED marker when absent."""
    try:
        from converge_orchestrator.config import evaluate_canonical_durable_control_state_paths
    except ImportError as exc:
        raise AssertionError(_GUARD_MISSING) from exc
    return evaluate_canonical_durable_control_state_paths


def _declaration(**overrides: Any) -> dict[str, Any]:
    """One durable control-state path declaration over a pinned, non-CWD authority."""
    declaration: dict[str, Any] = {
        "durable_control_state_path": _RELATIVE_PATH,
        "pinned_authority_source": {
            "kind": "pinned_project_config",
            "base": _PINNED_BASE,
        },
        "process_cwd": _PROCESS_CWD,
    }
    declaration.update(overrides)
    return declaration


def test_absolute_declaration_is_accepted() -> None:
    # An absolute declaration is already a canonical absolute identity and cannot
    # depend on the process CWD: the guard accepts it with no clause.
    declaration = _declaration(durable_control_state_path=_ABSOLUTE_PATH)
    assert _guard()(declaration) == ()


def test_absolute_declaration_accepted_from_other_pinned_source() -> None:
    # The requirement admits "another explicitly pinned authority source": an
    # absolute declaration pinned under the second declared source kind is
    # equally accepted.
    declaration = _declaration(
        durable_control_state_path=_ABSOLUTE_PATH,
        pinned_authority_source={
            "kind": "pinned_authority_source",
            "base": _PINNED_BASE,
        },
    )
    assert _guard()(declaration) == ()


def test_relative_declaration_anchored_off_cwd_is_accepted() -> None:
    # A relative declaration anchored to a pinned base whose canonical identity
    # differs from the process CWD resolves to one canonical absolute identity
    # without CWD dependence: the guard accepts it with no clause.
    assert _guard()(_declaration()) == ()


def test_relative_declaration_anchored_on_process_cwd_is_rejected() -> None:
    # A relative declaration whose pinned base is exactly the process CWD
    # resolves against the process CWD and is rejected naming the CWD dependence.
    declaration = _declaration(
        pinned_authority_source={
            "kind": "pinned_project_config",
            "base": _PROCESS_CWD,
        },
    )
    assert _guard()(declaration) == ("control_state_path_depends_on_process_cwd",)


def test_relative_declaration_anchored_on_canonical_process_cwd_is_rejected() -> None:
    # The process CWD under a canonical re-spelling (case-folded, trailing
    # slash folded) is the same canonical identity: the guard must still reject
    # the CWD-dependent anchoring.
    declaration = _declaration(
        pinned_authority_source={
            "kind": "pinned_project_config",
            "base": "/WORK/runtime/cwd/",
        },
    )
    assert _guard()(declaration) == ("control_state_path_depends_on_process_cwd",)


@pytest.mark.parametrize(
    "authority_source",
    [
        _MISSING,
        None,
        {},
        {"base": _PINNED_BASE},
        {"kind": "process_cwd", "base": _PINNED_BASE},
        {"kind": "", "base": _PINNED_BASE},
        {"kind": "   ", "base": _PINNED_BASE},
        {"kind": 123, "base": _PINNED_BASE},
        {"kind": "pinned_project_config"},
        {"kind": "pinned_project_config", "base": ""},
        {"kind": "pinned_project_config", "base": "relative/base"},
    ],
    ids=[
        "missing",
        "null",
        "empty-mapping",
        "kind-missing",
        "kind-unpinned",
        "kind-empty",
        "kind-blank",
        "kind-non-string",
        "base-missing",
        "base-empty",
        "base-non-absolute",
    ],
)
def test_relative_declaration_with_undeclared_pinned_authority_is_rejected(
    authority_source: Any,
) -> None:
    # Without a pinned, absolute-based authority source, a relative declaration
    # cannot resolve to a canonical absolute identity from a pinned source, so
    # the guard rejects it naming the undeclared authority source.
    declaration = _declaration()
    if authority_source is _MISSING:
        del declaration["pinned_authority_source"]
    else:
        declaration["pinned_authority_source"] = authority_source
    assert _guard()(declaration) == ("pinned_authority_source_undeclared",)


@pytest.mark.parametrize(
    "path_declaration",
    [
        _MISSING,
        None,
        "",
        "   ",
        123,
        [],
        "C:state/control",
        "//?/C:/state/control",
        "\\\\server\\share\\control",
    ],
    ids=[
        "missing",
        "null",
        "empty",
        "blank",
        "non-string-number",
        "non-string-list",
        "drive-relative",
        "verbatim",
        "unc",
    ],
)
def test_malformed_path_declaration_fails_closed(path_declaration: Any) -> None:
    # A missing or empty path declaration, or a path value that is not a plain
    # relative or absolute path, cannot resolve to one canonical absolute
    # identity: the guard fails closed with ValueError.
    declaration = _declaration()
    if path_declaration is _MISSING:
        del declaration["durable_control_state_path"]
    else:
        declaration["durable_control_state_path"] = path_declaration
    with pytest.raises(ValueError):
        _guard()(declaration)


@pytest.mark.parametrize(
    "process_cwd",
    [_MISSING, None, "", "   ", 123, "work/cwd"],
    ids=["missing", "null", "empty", "blank", "non-string", "non-absolute"],
)
def test_invalid_process_cwd_fails_closed(process_cwd: Any) -> None:
    # The process CWD must be declared and absolute for the guard to decide CWD
    # dependence: a missing or non-absolute process_cwd fails closed with
    # ValueError.
    declaration = _declaration()
    if process_cwd is _MISSING:
        del declaration["process_cwd"]
    else:
        declaration["process_cwd"] = process_cwd
    with pytest.raises(ValueError):
        _guard()(declaration)
