from __future__ import annotations

import json
import math
import threading
import uuid
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, Field

from .config import (
    _PROVIDER_ENDPOINT_CATEGORIES,
    _TRUSTED_PROVIDER_CLASSES,
    evaluate_provider_data_egress,
)
from .models import ProjectConfig, WorkflowState

_ESTIMATED_BYTES_PER_TOKEN = 3
_LEDGER_LOCK = threading.Lock()
_TRUNCATION_MARKER = "\n...[advisory truncated by deterministic context budget]"


@dataclass(frozen=True)
class AdvisorySection:
    """Context that may be compacted without weakening the authoritative task contract."""

    name: str
    text: str


@dataclass(frozen=True)
class PromptEnvelope:
    """Prompt split into immutable/core material and explicitly advisory context."""

    core: str
    advisory: tuple[AdvisorySection, ...] = ()


class ContextReport(BaseModel):
    invocation_id: str
    role: str
    session_mode: Literal["fresh"] = "fresh"
    budget_status: Literal[
        "bounded",
        "unknown_model_limit",
        "core_exceeded",
        "invalid_budget",
    ]
    context_limit_tokens: int | None = None
    output_reserve_tokens: int | None = None
    input_budget_tokens: int | None = None
    estimated_core_tokens: int = 0
    estimated_input_tokens: int = 0
    estimated_output_tokens: int | None = None
    advisory_included: list[str] = Field(default_factory=list)
    advisory_truncated: list[str] = Field(default_factory=list)
    advisory_dropped: list[str] = Field(default_factory=list)
    estimator: str = "ceil(utf8_bytes/3)"


class ContextBudgetExceeded(ValueError):
    def __init__(self, message: str, report: ContextReport):
        super().__init__(message)
        self.report = report


def estimate_tokens(text: str) -> int:
    """Conservative tokenizer-independent estimate used only for deterministic guardrails."""
    if not text:
        return 0
    return math.ceil(len(text.encode("utf-8")) / _ESTIMATED_BYTES_PER_TOKEN)


def _profile_limits(
    config: ProjectConfig,
    role: str,
    model_profile: str | None = None,
) -> tuple[int | None, int | None]:
    agent = config.agents[role]
    profile_name = model_profile or agent.model_profile
    if not profile_name:
        return None, None
    profile = config.model_profiles[profile_name]
    return profile.context_tokens, profile.output_tokens


def _input_budget(
    config: ProjectConfig,
    role: str,
    model_profile: str | None = None,
) -> tuple[int | None, int | None, str]:
    context_limit, configured_output = _profile_limits(config, role, model_profile)
    if context_limit is None:
        return None, None, "unknown_model_limit"
    output_reserve = max(config.context_output_reserve_tokens, configured_output or 0)
    available_after_output = context_limit - output_reserve
    fractional_limit = int(context_limit * config.context_input_fraction)
    budget = min(available_after_output, fractional_limit)
    if budget <= 0:
        return budget, output_reserve, "invalid_budget"
    return budget, output_reserve, "bounded"


def _section_text(section: AdvisorySection) -> str:
    return (
        f"\n\n{section.name.upper()} "
        "(ADVISORY; verify against repository and immutable requirements):\n"
        f"{section.text.strip()}\n"
    )


def _truncate_utf8(text: str, token_budget: int) -> str:
    if token_budget <= estimate_tokens(_TRUNCATION_MARKER):
        return ""
    byte_budget = token_budget * _ESTIMATED_BYTES_PER_TOKEN
    marker_bytes = len(_TRUNCATION_MARKER.encode("utf-8"))
    raw = text.encode("utf-8")
    prefix = raw[: max(0, byte_budget - marker_bytes)].decode("utf-8", errors="ignore")
    return prefix.rstrip() + _TRUNCATION_MARKER


def prepare_prompt(
    config: ProjectConfig,
    role: str,
    prompt: str | PromptEnvelope,
    *,
    model_profile: str | None = None,
) -> tuple[str, ContextReport]:
    envelope = prompt if isinstance(prompt, PromptEnvelope) else PromptEnvelope(core=prompt)
    invocation_id = uuid.uuid4().hex
    core = envelope.core.strip()
    core_tokens = estimate_tokens(core)
    input_budget, output_reserve, status = _input_budget(config, role, model_profile)
    context_limit, _ = _profile_limits(config, role, model_profile)
    report = ContextReport(
        invocation_id=invocation_id,
        role=role,
        budget_status=status,
        context_limit_tokens=context_limit,
        output_reserve_tokens=output_reserve,
        input_budget_tokens=input_budget,
        estimated_core_tokens=core_tokens,
        estimated_input_tokens=core_tokens,
    )

    if status == "invalid_budget":
        report.budget_status = "invalid_budget"
        message = (
            f"Invalid context budget for role {role}: "
            "context window is smaller than output reserve"
        )
        raise ContextBudgetExceeded(message, report)
    if input_budget is not None and core_tokens > input_budget:
        report.budget_status = "core_exceeded"
        raise ContextBudgetExceeded(
            f"Authoritative core context for role {role} requires ~{core_tokens} tokens, "
            f"above deterministic input budget {input_budget}; refusing silent truncation",
            report,
        )

    rendered = core
    for section in envelope.advisory:
        block = _section_text(section)
        block_tokens = estimate_tokens(block)
        if input_budget is None or report.estimated_input_tokens + block_tokens <= input_budget:
            rendered += block
            report.estimated_input_tokens += block_tokens
            report.advisory_included.append(section.name)
            continue

        remaining = input_budget - report.estimated_input_tokens
        header = _section_text(AdvisorySection(section.name, ""))
        header_tokens = estimate_tokens(header)
        compacted = _truncate_utf8(section.text.strip(), remaining - header_tokens)
        if compacted:
            compacted_block = _section_text(AdvisorySection(section.name, compacted))
            compacted_tokens = estimate_tokens(compacted_block)
            if report.estimated_input_tokens + compacted_tokens <= input_budget:
                rendered += compacted_block
                report.estimated_input_tokens += compacted_tokens
                report.advisory_truncated.append(section.name)
                continue
        report.advisory_dropped.append(section.name)

    return rendered, report


def finalize_report(report: ContextReport, output: str) -> ContextReport:
    return report.model_copy(update={"estimated_output_tokens": estimate_tokens(output)})


def append_context_ledger(config: ProjectConfig, report: ContextReport, cwd: Path) -> None:
    """Persist per-invocation context evidence without coupling it to chat/session history."""
    target = config.state_dir / "context-usage.jsonl"
    target.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "timestamp": datetime.now(UTC).isoformat(),
        "cwd": str(cwd.resolve()),
        **report.model_dump(mode="json"),
    }
    line = json.dumps(payload, ensure_ascii=False, sort_keys=True) + "\n"
    with _LEDGER_LOCK:
        with target.open("a", encoding="utf-8") as fh:
            fh.write(line)


def build_working_memory(state: WorkflowState) -> dict[str, Any]:
    """Deterministic bounded continuity artifact; never a replacement for requirements."""
    compliance = state.get("compliance") or {}
    entries = compliance.get("entries", {}) if isinstance(compliance, dict) else {}
    counts: dict[str, int] = {}
    unresolved: list[str] = []
    severity_by_id = {
        str(item.get("id")): str(item.get("severity", "mandatory"))
        for item in state.get("requirements", [])
        if isinstance(item, dict) and item.get("id")
    }
    # REQ-SOT-013: recommended records stay visible (ids only, sorted, bounded to 64).
    recommended_ids = sorted(rid for rid, sev in severity_by_id.items() if sev == "recommended")
    for requirement_id, raw in entries.items():
        if not isinstance(raw, dict):
            continue
        status = str(raw.get("status", "unverified"))
        counts[status] = counts.get(status, 0) + 1
        if status != "pass" and severity_by_id.get(str(requirement_id)) == "mandatory":
            unresolved.append(str(requirement_id))

    task = state.get("task") if isinstance(state.get("task"), dict) else None
    review = state.get("review_result") if isinstance(state.get("review_result"), dict) else None
    findings: list[dict[str, Any]] = []
    if review:
        for finding in review.get("findings", [])[:12]:
            if not isinstance(finding, dict):
                continue
            findings.append(
                {
                    "severity": finding.get("severity"),
                    "requirement_id": finding.get("requirement_id"),
                    "file": finding.get("file"),
                    "reason": str(finding.get("reason", ""))[:500],
                }
            )

    scout = (state.get("baseline") or {}).get("repo_scout")
    return {
        "source": "deterministic LangGraph state; advisory only",
        "immutable_requirements_remain_authoritative": True,
        "iteration": state.get("iteration", 0),
        "status": state.get("status"),
        "compliance_counts": counts,
        "unresolved_mandatory_requirement_ids": sorted(unresolved)[:64],
        "unresolved_ids_truncated": len(unresolved) > 64,
        "recommended_requirement_ids": recommended_ids[:64],
        "recommended_ids_truncated": len(recommended_ids) > 64,
        "last_task": (
            {
                "id": task.get("id"),
                "title": task.get("title"),
                "requirement_ids": list(task.get("requirement_ids", []))[:32],
                "objective": str(task.get("objective", ""))[:1000],
            }
            if task
            else None
        ),
        "repair_attempts": state.get("repair_attempts", 0),
        "replan_attempts": state.get("replan_attempts", 0),
        "last_review_findings": findings,
        "ci_status": (state.get("ci") or {}).get("status") if state.get("ci") else None,
        "repo_scout": (
            {
                "source": scout.get("source"),
                "base_commit": scout.get("base_commit"),
                "warnings": list(scout.get("warnings", []))[:8],
            }
            if isinstance(scout, dict)
            else None
        ),
    }


def evaluate_context_provider_trust(
    repository_derived: bool,
    endpoint_category: str,
    provider_trust: dict[str, Any] | None = None,
) -> tuple[str, ...]:
    """Enforce REQ-DATA-001: fail-closed guard for repository-derived content egress.

    Returns an empty tuple only when repository-derived content targets a category
    whose ``trust_class`` is ``local`` or ``trusted`` and ``repository_data_egress``
    is explicitly ``True``. Reports the violated clauses otherwise and raises
    ``ValueError`` for a malformed or empty ``provider_trust`` mapping, unknown
    endpoint category, or non-mapping category declaration.
    """
    if not repository_derived:
        return ()

    # Fail-closed structural validation is single-sourced from REQ-CONFIG-014.
    evaluate_provider_data_egress({"provider_trust": provider_trust})
    assert isinstance(provider_trust, dict)

    if endpoint_category not in _PROVIDER_ENDPOINT_CATEGORIES:
        raise ValueError(f"unknown endpoint category: {endpoint_category!r}")

    declaration = provider_trust[endpoint_category]
    trust_class = declaration.get("trust_class")
    egress = declaration.get("repository_data_egress")

    clauses: list[str] = []
    if not isinstance(trust_class, str) or not trust_class.strip():
        clauses.append(f"context_provider_trust_class_undeclared:{endpoint_category}")
    elif trust_class not in _TRUSTED_PROVIDER_CLASSES:
        clauses.append(
            f"repository_derived_content_transmitted_to_untrusted_provider:{endpoint_category}"
        )
    if egress is not True:
        clauses.append(f"repository_derived_content_egress_not_permitted:{endpoint_category}")
    return tuple(clauses)


def _is_non_blank_string(value: Any) -> bool:
    """Return True only for non-empty, non-whitespace strings."""
    return isinstance(value, str) and value.strip() != ""


def select_authorized_fallback_endpoints(
    role: Any, endpoints: Any
) -> tuple[dict[str, Any], ...]:
    """Return fallback endpoints authorized to receive the complete context for ``role``.

    REQ-DATA-005: a fallback endpoint that is not authorized to receive the complete
    authoritative context required for the role SHALL be skipped. The selector is
    pure, offline, and deterministic: it consumes explicit caller-supplied endpoint
    declarations and returns, in declaration order, only those whose
    ``authorized_roles`` include the role and whose
    ``receives_complete_authoritative_context`` is explicitly ``True``.

    Malformed ``role`` values, non-iterable ``endpoints``, non-mapping endpoint
    declarations, missing or malformed ``authorized_roles``, and any context flag
    other than the explicit boolean ``True`` or ``False`` fail closed with
    ``ValueError``. An endpoint declaring ``receives_complete_authoritative_context``
    as ``False`` is skipped rather than treated as authorized.
    """
    if not _is_non_blank_string(role):
        raise ValueError(f"role must be a non-blank string: {role!r}")

    if isinstance(endpoints, (str, bytes)) or not isinstance(endpoints, Iterable):
        raise ValueError(f"endpoints must be an iterable of mappings: {endpoints!r}")

    selected: list[dict[str, Any]] = []
    for endpoint in endpoints:
        if not isinstance(endpoint, dict):
            raise ValueError(f"each endpoint must be a mapping: {endpoint!r}")

        authorized_roles = endpoint.get("authorized_roles")
        if (
            authorized_roles is None
            or isinstance(authorized_roles, (str, bytes, Mapping))
            or not isinstance(authorized_roles, Iterable)
        ):
            raise ValueError(
                f"authorized_roles must be an iterable of non-blank strings: "
                f"{authorized_roles!r}"
            )

        roles = tuple(authorized_roles)
        for authorized_role in roles:
            if not _is_non_blank_string(authorized_role):
                raise ValueError(
                    f"authorized_roles must be an iterable of non-blank strings: "
                    f"{authorized_roles!r}"
                )

        receives_complete = endpoint.get("receives_complete_authoritative_context")
        if receives_complete is True:
            if role in roles:
                selected.append(endpoint)
        elif receives_complete is False:
            # Well-formed declaration of incomplete context authorization: skip.
            continue
        else:
            raise ValueError(
                f"receives_complete_authoritative_context must be a boolean "
                f"True or False: {receives_complete!r}"
            )

    return tuple(selected)


def evaluate_authorized_fallback_non_pass(
    role: Any, endpoints: Any
) -> tuple[str, ...]:
    """Return a non-PASS evidence clause unless an authorized complete-context fallback exists.

    REQ-DATA-006: when no authorized fallback can receive the complete authoritative
    context required for the role, the operation SHALL remain non-PASS rather than
    sending restricted content or weakening authoritative context.

    The evaluator is pure, offline, and deterministic. It returns an empty tuple
    only when ``select_authorized_fallback_endpoints`` finds at least one fallback
    endpoint declaration that is authorized for ``role`` and explicitly declares
    ``receives_complete_authoritative_context`` as ``True``. Otherwise it returns
    the single clause
    ``no_authorized_fallback_receives_complete_authoritative_context:<role>``.

    Malformed ``role`` values and malformed ``endpoints`` declarations fail closed
    with ``ValueError`` by delegating validation to the selector.
    """
    selected = select_authorized_fallback_endpoints(role, endpoints)
    if selected:
        return ()
    return (f"no_authorized_fallback_receives_complete_authoritative_context:{role}",)
