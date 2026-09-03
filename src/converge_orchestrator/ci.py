from __future__ import annotations

from datetime import UTC, datetime, timedelta

from langgraph.types import interrupt

from . import workflow as wf
from .config import load_config
from .github import GitHubAdapter
from .models import TaskEnvelope, WorkflowState


def _utcnow() -> datetime:
    return datetime.now(UTC)


def _parse_timestamp(value: str) -> datetime:
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def ci_poll(state: WorkflowState) -> WorkflowState:
    """Observe GitHub CI exactly once and checkpoint the observation in graph state."""
    cfg = load_config(state["config_path"])
    task = TaskEnvelope.model_validate(state["task"])
    pr = state["pr"]
    head_sha = str(pr["head_sha"])
    now = _utcnow()

    previous_head = state.get("ci_head_sha")
    raw_started_at = state.get("ci_started_at")
    if previous_head != head_sha or not raw_started_at:
        started_at = now
    else:
        started_at = _parse_timestamp(str(raw_started_at))

    result = GitHubAdapter(cfg).ci_status(head_sha)
    if (
        result.status == "pending"
        and (now - started_at).total_seconds() >= cfg.ci_timeout_seconds
    ):
        result = result.model_copy(update={"status": "timeout"})

    payload = result.model_dump(mode="json")
    store = wf._evidence(state)
    store.write_json(state["run_id"], task.id, "ci.json", payload)
    store.append_event(
        state["run_id"],
        "ci_poll",
        {
            "task_id": task.id,
            "head_sha": head_sha,
            "status": result.status,
            "started_at": started_at.isoformat(),
            "observed_at": now.isoformat(),
        },
    )
    return {
        **state,
        "ci": payload,
        "ci_head_sha": head_sha,
        "ci_started_at": started_at.isoformat(),
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

    cfg = load_config(state["config_path"])
    if state.get("repair_attempts", 0) < cfg.max_repair_attempts:
        return "repair"
    if state.get("replan_attempts", 0) < cfg.max_replans:
        return "replan"
    return "human"
