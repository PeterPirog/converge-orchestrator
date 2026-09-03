from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

from langgraph.types import interrupt

from . import workflow as wf
from .ci_flakes import GitHubFlakyCIAdapter as GitHubAdapter
from .ci_flakes import (
    choose_flaky_retry,
    load_flaky_ci_policy,
    retry_error_text,
    retry_evidence,
)
from .config import load_config
from .github import GitHubError
from .models import TaskEnvelope, WorkflowState

_FLAKY_RETRY_LEDGER = "ci-flaky-retries.json"


def _utcnow() -> datetime:
    return datetime.now(UTC)


def _parse_timestamp(value: str) -> datetime:
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def _normalized_retry_counts(raw: Any) -> dict[str, int]:
    if not isinstance(raw, dict):
        return {}
    counts: dict[str, int] = {}
    for key, value in raw.items():
        try:
            count = int(value)
        except (TypeError, ValueError):
            continue
        if count >= 0:
            counts[str(key)] = count
    return counts


def _ci_retry_counts(state: WorkflowState, head_sha: str) -> dict[str, int]:
    if state.get("ci_head_sha") != head_sha:
        return {}
    return _normalized_retry_counts(state.get("ci_flaky_retries"))  # type: ignore[typeddict-item]


def _durable_retry_counts(
    store: Any,
    run_id: str,
    task_id: str,
    head_sha: str,
) -> dict[str, int]:
    ledger = store.read_json(run_id, task_id, _FLAKY_RETRY_LEDGER)
    if not isinstance(ledger, dict) or ledger.get("head_sha") != head_sha:
        return {}
    return _normalized_retry_counts(ledger.get("counts"))


def _merge_retry_counts(
    state_counts: dict[str, int],
    durable_counts: dict[str, int],
) -> dict[str, int]:
    names = set(state_counts) | set(durable_counts)
    return {
        name: max(state_counts.get(name, 0), durable_counts.get(name, 0))
        for name in sorted(names)
    }


def _reserve_retry(
    store: Any,
    run_id: str,
    task_id: str,
    head_sha: str,
    retry_counts: dict[str, int],
    check_name: str,
) -> int:
    retry_count = retry_counts.get(check_name, 0) + 1
    retry_counts[check_name] = retry_count
    store.write_json(
        run_id,
        task_id,
        _FLAKY_RETRY_LEDGER,
        {"head_sha": head_sha, "counts": retry_counts},
    )
    return retry_count


def ci_poll(state: WorkflowState) -> WorkflowState:
    """Observe CI once; only exact, explicitly flaky Actions jobs may be rerun automatically."""
    cfg = load_config(state["config_path"])
    flaky_policy = load_flaky_ci_policy(state["config_path"])
    task = TaskEnvelope.model_validate(state["task"])
    pr = state["pr"]
    head_sha = str(pr["head_sha"])
    now = _utcnow()
    store = wf._evidence(state)

    previous_head = state.get("ci_head_sha")
    raw_started_at = state.get("ci_started_at")
    if previous_head != head_sha or not raw_started_at:
        started_at = now
    else:
        started_at = _parse_timestamp(str(raw_started_at))
    elapsed = (now - started_at).total_seconds()

    state_retry_counts = _ci_retry_counts(state, head_sha)
    durable_retry_counts = _durable_retry_counts(
        store,
        state["run_id"],
        task.id,
        head_sha,
    )
    retry_counts = _merge_retry_counts(state_retry_counts, durable_retry_counts)
    recovered_reservation = any(
        durable_retry_counts.get(name, 0) > state_retry_counts.get(name, 0)
        for name in durable_retry_counts
    )

    adapter = GitHubAdapter(cfg)
    result = adapter.ci_status(head_sha)
    retry_error: str | None = None
    retry_record: dict[str, Any] | None = None
    recovery_record: dict[str, Any] | None = None

    if result.status == "pending" and elapsed >= cfg.ci_timeout_seconds:
        result = result.model_copy(update={"status": "timeout"})
    elif result.status == "fail" and elapsed < cfg.ci_timeout_seconds:
        if recovered_reservation:
            recovery_record = {
                "kind": "flaky_ci_recovery_wait",
                "head_sha": head_sha,
                "retry_counts": retry_counts,
            }
            result = result.model_copy(
                update={
                    "status": "pending",
                    "checks": [*result.checks, recovery_record],
                }
            )
        else:
            check_name = choose_flaky_retry(result, flaky_policy, retry_counts)
            if check_name is not None:
                retry_count = _reserve_retry(
                    store,
                    state["run_id"],
                    task.id,
                    head_sha,
                    retry_counts,
                    check_name,
                )
                try:
                    job_id = adapter.rerun_failed_actions_check(head_sha, check_name)
                except GitHubError as exc:
                    retry_error = retry_error_text(exc)
                else:
                    retry_record = retry_evidence(
                        check_name=check_name,
                        job_id=job_id,
                        retry_count=retry_count,
                        head_sha=head_sha,
                    )
                    result = result.model_copy(
                        update={
                            "status": "pending",
                            "checks": [*result.checks, retry_record],
                        }
                    )

    payload = result.model_dump(mode="json")
    store.write_json(state["run_id"], task.id, "ci.json", payload)
    if retry_record is not None:
        store.append_event(state["run_id"], "ci_flaky_retry", retry_record)
    if recovery_record is not None:
        store.append_event(state["run_id"], "ci_flaky_recovery_wait", recovery_record)
    if retry_error is not None:
        store.append_event(
            state["run_id"],
            "ci_flaky_retry_error",
            {
                "task_id": task.id,
                "head_sha": head_sha,
                "error": retry_error,
            },
        )
    store.append_event(
        state["run_id"],
        "ci_poll",
        {
            "task_id": task.id,
            "head_sha": head_sha,
            "status": result.status,
            "started_at": started_at.isoformat(),
            "observed_at": now.isoformat(),
            "flaky_retry": retry_record,
            "flaky_recovery_wait": recovery_record,
        },
    )
    return {
        **state,
        "ci": payload,
        "ci_head_sha": head_sha,
        "ci_started_at": started_at.isoformat(),
        "ci_flaky_retries": retry_counts,
        "ci_flaky_retry_error": retry_error,
        "status": f"ci_{result.status}",
    }


def ci_wait(state: WorkflowState) -> WorkflowState:
    """Suspend the graph without holding a worker until the next configured CI poll."""
    cfg = load_config(state["config_path"])
    ci = state.get("ci") or {}
    if ci.get("status") != "pending":
        raise RuntimeError("ci_wait is only valid after a pending CI observation")

    wake_at = (_utcnow() + timedelta(seconds=cfg.ci_poll_seconds)).isoformat()
    decision = interrupt(
        {
            "kind": "ci_wait",
            "run_id": state["run_id"],
            "head_sha": ci.get("head_sha"),
            "wake_at": wake_at,
            "poll_seconds": cfg.ci_poll_seconds,
        }
    )
    action = decision.get("action") if isinstance(decision, dict) else decision
    if action not in {"resume", "poll"}:
        raise ValueError(f"Unsupported CI wait resume action: {action}")
    return {**state, "status": "ci_wait_elapsed"}


def route_after_ci(state: WorkflowState) -> str:
    ci = state.get("ci") or {}
    status = ci.get("status")
    if status == "pending":
        return "wait"
    if status == "pass":
        cfg = load_config(state["config_path"])
        return "merge" if cfg.auto_merge else "end"
    if state.get("ci_flaky_retry_error"):  # type: ignore[typeddict-item]
        return "human"

    cfg = load_config(state["config_path"])
    if state.get("repair_attempts", 0) < cfg.max_repair_attempts:
        return "repair"
    if state.get("replan_attempts", 0) < cfg.max_replans:
        return "replan"
    return "human"
