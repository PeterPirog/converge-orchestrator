from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any
from uuid import uuid4

from langgraph.graph import END, START, StateGraph
from langgraph.types import interrupt

from .compliance import ComplianceEngine
from .config import load_config
from .control import ControlSignals
from .evidence import EvidenceStore
from .git import (
    cleanup_worktree,
    commit_all,
    create_worktree,
    delete_remote_branch,
    diff,
    ensure_clean,
    existing_candidate_commit,
    push,
    update_base,
)
from .github import GitHubAdapter
from .models import (
    ComplianceSnapshot,
    GateResult,
    Requirement,
    RequirementStatus,
    ReviewResult,
    TaskEnvelope,
    WorkflowState,
)
from .opencode import OpenCodeAdapter
from .policy import BLOCKING_RISK_FLAGS, DecisionKind, can_integrate
from .prompts import builder_prompt, planner_prompt, repair_prompt, reviewer_prompt
from .quality import required_gates_pass, run_quality_gates, run_scope_gate
from .risk import classify_repository_risk
from .spec import compile_contract, is_read_only, sha256_file, write_contract


def _json_object(text: str) -> dict[str, Any]:
    stripped = text.strip()
    try:
        payload = json.loads(stripped)
    except json.JSONDecodeError:
        start = stripped.find("{")
        end = stripped.rfind("}")
        if start < 0 or end <= start:
            raise ValueError("Agent did not return a JSON object") from None
        payload = json.loads(stripped[start : end + 1])
    if not isinstance(payload, dict):
        raise ValueError("Agent output must be a JSON object")
    return payload


def _evidence(state: WorkflowState) -> EvidenceStore:
    cfg = load_config(state["config_path"])
    return EvidenceStore(cfg.state_dir / "evidence")


def _task_id(state: WorkflowState) -> str:
    task = state.get("task")
    return str(task.get("id")) if task else "run"


def _requirements(state: WorkflowState) -> list[Requirement]:
    return [Requirement.model_validate(item) for item in state["requirements"]]


def _write_compliance(state: WorkflowState, compliance: ComplianceSnapshot) -> None:
    cfg = load_config(state["config_path"])
    path = cfg.state_dir / "compliance.json"
    temporary = path.with_suffix(".json.tmp")
    temporary.write_text(
        compliance.model_dump_json(indent=2) + "\n",
        encoding="utf-8",
    )
    temporary.replace(path)


def _load_compliance(cfg: Any, contract: Any) -> ComplianceSnapshot:
    baseline = ComplianceEngine.initial(contract)
    path = cfg.state_dir / "compliance.json"
    if not path.is_file():
        return baseline
    try:
        persisted = ComplianceSnapshot.model_validate_json(path.read_text(encoding="utf-8"))
    except ValueError as exc:
        raise RuntimeError("Persisted compliance state is invalid") from exc
    if set(persisted.entries) - set(baseline.entries):
        raise RuntimeError(
            "Persisted compliance does not match the immutable requirements contract"
        )
    for requirement_id, entry in persisted.entries.items():
        baseline.entries[requirement_id] = entry
    baseline.mandatory_regressions = persisted.mandatory_regressions
    return baseline


def bootstrap(state: WorkflowState) -> WorkflowState:
    cfg = load_config(state["config_path"])
    if not cfg.repo_path.exists() or not (cfg.repo_path / ".git").exists():
        raise RuntimeError(f"Not a git repository: {cfg.repo_path}")
    if not cfg.requirements_path.is_file():
        raise RuntimeError(f"Requirements file not found: {cfg.requirements_path}")
    if cfg.require_spec_read_only and not is_read_only(cfg.requirements_path):
        raise RuntimeError("Requirements file must be read-only before orchestration starts.")
    ensure_clean(cfg.repo_path)
    expected = sha256_file(cfg.requirements_path)
    hash_path = cfg.state_dir / "requirements.sha256"
    if hash_path.exists() and hash_path.read_text(encoding="utf-8").strip() != expected:
        raise RuntimeError(
            "Requirements changed since project initialization; refusing to continue."
        )
    hash_path.write_text(expected, encoding="utf-8")
    contract = compile_contract(cfg.requirements_path)
    write_contract(cfg.state_dir / "contract.json", contract)
    compliance = _load_compliance(cfg, contract)
    run_id = state.get("run_id") or uuid4().hex
    store = EvidenceStore(cfg.state_dir / "evidence")
    store.append_event(
        run_id,
        "bootstrap",
        {"requirements_sha256": expected, "requirements": len(contract.requirements)},
    )
    next_state: WorkflowState = {
        **state,
        "run_id": run_id,
        "requirements_hash": expected,
        "requirements": [item.model_dump(mode="json") for item in contract.requirements],
        "compliance": compliance.model_dump(mode="json"),
        "iteration": state.get("iteration", 0),
        "repair_attempts": 0,
        "replan_attempts": 0,
        "risk_flags": [],
        "approved_risk_flags": [],
        "risk_report": None,
        "risk_fingerprint": None,
        "status": "bootstrapped",
    }
    _write_compliance(next_state, compliance)
    return next_state


def guard_spec(state: WorkflowState) -> WorkflowState:
    cfg = load_config(state["config_path"])
    current = sha256_file(cfg.requirements_path)
    if current != state["requirements_hash"]:
        _evidence(state).append_event(
            state["run_id"],
            "spec_changed",
            {"expected": state["requirements_hash"], "actual": current},
        )
        return {
            **state,
            "status": "spec_changed",
            "message": "Immutable architecture specification changed; run stopped.",
        }
    return {**state, "status": "spec_ok"}


def route_after_guard(state: WorkflowState) -> str:
    return "continue" if state.get("status") == "spec_ok" else "end"


def spec_stop(state: WorkflowState) -> WorkflowState:
    return {
        **state,
        "status": "spec_changed",
        "message": "Immutable architecture specification changed; run stopped.",
    }


def _safe_point(state: WorkflowState, name: str) -> WorkflowState:
    cfg = load_config(state["config_path"])
    signals = ControlSignals(cfg.state_dir)
    if not signals.pause_requested(state["run_id"]):
        return state
    decision = interrupt(
        {
            "kind": "controlled_pause",
            "run_id": state["run_id"],
            "safe_point": name,
            "task": state.get("task"),
            "status": state.get("status"),
            "allowed": ["resume", "stop"],
        }
    )
    action = decision.get("action") if isinstance(decision, dict) else decision
    signals.clear_pause(state["run_id"])
    if action == "stop":
        return {**state, "status": "stopped", "message": "Stopped at controlled pause"}
    if action != "resume":
        raise ValueError(f"Unsupported pause decision: {action}")
    return {**state, "status": f"resumed_at_{name}"}


def pause_before_plan(state: WorkflowState) -> WorkflowState:
    return _safe_point(state, "before_plan")


def pause_before_build(state: WorkflowState) -> WorkflowState:
    return _safe_point(state, "before_build")


def pause_before_repair(state: WorkflowState) -> WorkflowState:
    return _safe_point(state, "before_repair")


def pause_before_integrate(state: WorkflowState) -> WorkflowState:
    return _safe_point(state, "before_integrate")


def pause_before_pr(state: WorkflowState) -> WorkflowState:
    return _safe_point(state, "before_pr")


def pause_before_merge(state: WorkflowState) -> WorkflowState:
    return _safe_point(state, "before_merge")


def route_after_pause(state: WorkflowState) -> str:
    return "end" if state.get("status") == "stopped" else "continue"


def plan(state: WorkflowState) -> WorkflowState:
    cfg = load_config(state["config_path"])
    update_base(cfg.repo_path, cfg.base_branch)
    requirements = _requirements(state)
    result = OpenCodeAdapter(cfg).invoke(
        "planner",
        planner_prompt(requirements, state["iteration"] + 1),
        cfg.repo_path,
    )
    if not result.ok:
        raise RuntimeError(f"Planner failed: {result.output}")
    task = TaskEnvelope.model_validate(_json_object(result.output))
    known_ids = {item.id for item in requirements}
    unknown_ids = set(task.requirement_ids) - known_ids
    if unknown_ids:
        raise ValueError(f"Planner returned unknown requirement IDs: {sorted(unknown_ids)}")
    next_state: WorkflowState = {
        **state,
        "task": task.model_dump(mode="json"),
        "iteration": state["iteration"] + 1,
        "risk_flags": task.risk_flags,
        "approved_risk_flags": [],
        "risk_report": None,
        "risk_fingerprint": None,
        "status": "planned",
    }
    store = _evidence(next_state)
    store.write_json(next_state["run_id"], task.id, "task.json", next_state["task"])
    store.append_event(next_state["run_id"], "planned", {"task_id": task.id})
    return next_state


def prepare_worktree(state: WorkflowState) -> WorkflowState:
    cfg = load_config(state["config_path"])
    task = TaskEnvelope.model_validate(state["task"])
    worktree, branch = create_worktree(
        cfg.repo_path,
        cfg.worktree_dir,
        task.id,
        cfg.base_branch,
        cfg.branch_prefix,
    )
    return {
        **state,
        "worktree": str(worktree),
        "branch": branch,
        "status": "worktree_ready",
    }


def build(state: WorkflowState) -> WorkflowState:
    cfg = load_config(state["config_path"])
    task = TaskEnvelope.model_validate(state["task"])
    result = OpenCodeAdapter(cfg).invoke(
        "builder",
        builder_prompt(task, _requirements(state)),
        Path(state["worktree"]),
    )
    return {
        **state,
        "message": result.output,
        "status": "built" if result.ok else "builder_failed",
    }


def quality(state: WorkflowState) -> WorkflowState:
    cfg = load_config(state["config_path"])
    task = TaskEnvelope.model_validate(state["task"])
    worktree = Path(state["worktree"])
    results = [run_scope_gate(cfg, worktree, task), *run_quality_gates(cfg, worktree)]
    payload = [item.model_dump(mode="json") for item in results]
    store = _evidence(state)
    store.write_json(state["run_id"], task.id, "quality.json", payload)
    return {**state, "quality_results": payload, "status": "quality_checked"}


def review(state: WorkflowState) -> WorkflowState:
    cfg = load_config(state["config_path"])
    task = TaskEnvelope.model_validate(state["task"])
    requirements = _requirements(state)
    worktree = Path(state["worktree"])
    patch = diff(worktree, cfg.base_branch)
    fingerprint = hashlib.sha256(patch.encode("utf-8")).hexdigest()
    report = classify_repository_risk(cfg, worktree, task)
    risk_report = report.model_dump(mode="json")
    declared_risk_flags = set(task.risk_flags) - BLOCKING_RISK_FLAGS
    risk_flags = sorted(declared_risk_flags | set(report.flags))
    same_candidate = state.get("risk_fingerprint") == fingerprint
    approved_risk_flags = (
        list(state.get("approved_risk_flags", [])) if same_candidate else []
    )

    store = _evidence(state)
    store.write_json(state["run_id"], task.id, "risk.json", risk_report)
    store.append_event(
        state["run_id"],
        "risk_classified",
        {
            "task_id": task.id,
            "flags": risk_flags,
            "findings": len(report.findings),
            "candidate_sha256": fingerprint,
        },
    )

    blocking_flags = sorted(BLOCKING_RISK_FLAGS.intersection(report.flags))
    if blocking_flags:
        review_result = ReviewResult(
            verdict="reject",
            findings=[
                {
                    "severity": "blocker",
                    "reason": (
                        "Deterministic repository-risk policy blocked semantic review: "
                        + ", ".join(blocking_flags)
                    ),
                    "required_fix": (
                        "Remove blocking material before any external semantic reviewer receives "
                        "the candidate diff."
                    ),
                }
            ],
        )
        store.write_json(
            state["run_id"],
            task.id,
            "review.json",
            review_result.model_dump(mode="json"),
        )
        return {
            **state,
            "risk_flags": risk_flags,
            "approved_risk_flags": approved_risk_flags,
            "risk_report": risk_report,
            "risk_fingerprint": fingerprint,
            "review_result": review_result.model_dump(mode="json"),
            "status": "risk_blocked",
        }

    result = OpenCodeAdapter(cfg).invoke(
        "reviewer",
        reviewer_prompt(task, patch, requirements),
        worktree,
    )
    if result.ok:
        review_result = ReviewResult.model_validate(_json_object(result.output))
    else:
        review_result = ReviewResult(
            verdict="reject",
            findings=[
                {
                    "severity": "major",
                    "reason": result.output,
                    "required_fix": "Reviewer execution must succeed",
                }
            ],
        )
    store.write_text(state["run_id"], task.id, "diff.patch", patch)
    store.write_json(
        state["run_id"],
        task.id,
        "review.json",
        review_result.model_dump(mode="json"),
    )
    return {
        **state,
        "risk_flags": risk_flags,
        "approved_risk_flags": approved_risk_flags,
        "risk_report": risk_report,
        "risk_fingerprint": fingerprint,
        "review_result": review_result.model_dump(mode="json"),
        "status": "reviewed",
    }


def _integration_decision(state: WorkflowState):
    cfg = load_config(state["config_path"])
    gates = [GateResult.model_validate(item) for item in state.get("quality_results", [])]
    review_result = ReviewResult.model_validate(state["review_result"])
    compliance = ComplianceSnapshot.model_validate(state["compliance"])
    approved = set(state.get("approved_risk_flags", []))
    active_risks = [flag for flag in state.get("risk_flags", []) if flag not in approved]
    return can_integrate(
        expected_spec_hash=state["requirements_hash"],
        current_spec_hash=sha256_file(cfg.requirements_path),
        gates=gates,
        review=review_result,
        compliance=compliance,
        risk_flags=active_risks,
    )


def route_after_review(state: WorkflowState) -> str:
    cfg = load_config(state["config_path"])
    decision = _integration_decision(state)
    if decision.kind == DecisionKind.ALLOW:
        return "integrate"
    if decision.reason == "SPEC_CHANGED":
        return "spec_stop"
    if decision.kind == DecisionKind.INTERRUPT:
        return "human"
    if state.get("repair_attempts", 0) < cfg.max_repair_attempts:
        return "repair"
    if state.get("replan_attempts", 0) < cfg.max_replans:
        return "replan"
    return "human"


def repair(state: WorkflowState) -> WorkflowState:
    cfg = load_config(state["config_path"])
    task = TaskEnvelope.model_validate(state["task"])
    result = OpenCodeAdapter(cfg).invoke(
        "builder",
        repair_prompt(
            task,
            _requirements(state),
            state.get("quality_results", []),
            state.get("review_result"),
        ),
        Path(state["worktree"]),
    )
    return {
        **state,
        "repair_attempts": state.get("repair_attempts", 0) + 1,
        "message": result.output,
        "status": "repaired" if result.ok else "repair_failed",
    }


def _discard_current_workspace(state: WorkflowState) -> None:
    cfg = load_config(state["config_path"])
    branch = state.get("branch")
    worktree = state.get("worktree")
    if state.get("pr") and cfg.github_repo:
        GitHubAdapter(cfg).close_pull_request(int(state["pr"]["number"]))
    if branch:
        delete_remote_branch(cfg.repo_path, branch)
    if branch and worktree:
        cleanup_worktree(cfg.repo_path, Path(worktree), branch)


def replan(state: WorkflowState) -> WorkflowState:
    _discard_current_workspace(state)
    store = _evidence(state)
    store.append_event(
        state["run_id"],
        "replan",
        {"task_id": _task_id(state), "attempt": state.get("replan_attempts", 0) + 1},
    )
    return {
        **state,
        "task": None,
        "worktree": None,
        "branch": None,
        "quality_results": [],
        "review_result": None,
        "commit_sha": None,
        "pr": None,
        "ci": None,
        "repair_attempts": 0,
        "replan_attempts": state.get("replan_attempts", 0) + 1,
        "risk_flags": [],
        "approved_risk_flags": [],
        "risk_report": None,
        "risk_fingerprint": None,
        "status": "replanning",
    }


def _human_kind(state: WorkflowState) -> str:
    if state.get("status") in {"ci_fail", "ci_timeout"}:
        return "ci_failure_budget"
    if state.get("status") == "iteration_budget_exhausted":
        return "iteration_budget"
    if state.get("review_result"):
        decision = _integration_decision(state)
        if decision.kind == DecisionKind.INTERRUPT:
            return "risk_policy"
    return "repair_replan_budget"


def human_gate(state: WorkflowState) -> WorkflowState:
    kind = _human_kind(state)
    if kind == "risk_policy":
        allowed = ["approve", "edit", "reject"]
    else:
        allowed = ["retry", "edit", "reject"]
    decision = interrupt(
        {
            "kind": kind,
            "reason": "Autonomous policy requires human intervention",
            "task": state.get("task"),
            "quality": state.get("quality_results"),
            "review": state.get("review_result"),
            "ci": state.get("ci"),
            "risk_flags": state.get("risk_flags"),
            "risk_report": state.get("risk_report"),
            "risk_fingerprint": state.get("risk_fingerprint"),
            "allowed": allowed,
        }
    )
    payload = decision if isinstance(decision, dict) else {"action": decision}
    action = payload.get("action")
    if action in {"reject", "stop"}:
        return {**state, "status": "stopped", "message": "Stopped by human operator"}
    if action == "approve":
        if kind != "risk_policy":
            raise ValueError("Human approval cannot override deterministic gate or CI failures")
        approved = sorted(
            set(state.get("approved_risk_flags", [])) | set(state.get("risk_flags", []))
        )
        return {
            **state,
            "approved_risk_flags": approved,
            "status": "human_retry_review",
        }
    if action == "retry":
        if kind == "iteration_budget":
            return {**state, "iteration": 0, "status": "human_retry_plan"}
        return {
            **state,
            "repair_attempts": 0,
            "replan_attempts": 0,
            "status": (
                "human_retry_ci"
                if kind == "ci_failure_budget"
                else "human_retry_review"
            ),
        }
    if action == "edit":
        raw_task = payload.get("task")
        if not raw_task:
            raise ValueError("edit decision requires a replacement task envelope")
        task = TaskEnvelope.model_validate(raw_task)
        known_ids = {item["id"] for item in state["requirements"]}
        if set(task.requirement_ids) - known_ids:
            raise ValueError("edited task contains unknown requirement IDs")
        _discard_current_workspace(state)
        return {
            **state,
            "task": task.model_dump(mode="json"),
            "worktree": None,
            "branch": None,
            "quality_results": [],
            "review_result": None,
            "commit_sha": None,
            "pr": None,
            "ci": None,
            "repair_attempts": 0,
            "replan_attempts": 0,
            "risk_flags": task.risk_flags,
            "approved_risk_flags": [],
            "risk_report": None,
            "risk_fingerprint": None,
            "status": "human_edit",
        }
    raise ValueError(f"Unsupported human decision: {action}")


def route_after_human(state: WorkflowState) -> str:
    status = state.get("status")
    if status == "human_retry_ci":
        return "repair"
    if status == "human_edit":
        return "prepare"
    if status == "human_retry_plan":
        return "plan"
    if status == "human_retry_review":
        return "review"
    return "end"


def integrate(state: WorkflowState) -> WorkflowState:
    cfg = load_config(state["config_path"])
    if sha256_file(cfg.requirements_path) != state["requirements_hash"]:
        return spec_stop(state)
    task = TaskEnvelope.model_validate(state["task"])
    worktree = Path(state["worktree"])
    created_commit = commit_all(worktree, f"feat: {task.title}")
    commit = created_commit or existing_candidate_commit(worktree, cfg.base_branch)
    if commit:
        push(worktree, state["branch"])
    store = _evidence(state)
    store.append_event(
        state["run_id"],
        "pushed",
        {
            "task_id": task.id,
            "branch": state["branch"],
            "commit_sha": commit,
            "recovered_existing_commit": bool(commit and not created_commit),
        },
    )
    compliance = ComplianceSnapshot.model_validate(state["compliance"])
    gates = [GateResult.model_validate(item) for item in state["quality_results"]]
    if required_gates_pass(gates):
        compliance = ComplianceEngine.mark_local_pass(
            compliance,
            task.requirement_ids,
            [f"local-gates:{state['run_id']}:{task.id}", "independent-review:pass"],
        )
    next_state: WorkflowState = {
        **state,
        "commit_sha": commit,
        "compliance": compliance.model_dump(mode="json"),
        "status": "pushed" if commit else "no_changes",
        "message": commit or "No changes produced",
    }
    _write_compliance(next_state, compliance)
    return next_state


def route_after_integrate(state: WorkflowState) -> str:
    cfg = load_config(state["config_path"])
    if state.get("status") == "spec_changed" or not state.get("commit_sha"):
        return "end"
    return "pr" if cfg.github_repo else "end"


def _pull_request_body(state: WorkflowState) -> str:
    task = TaskEnvelope.model_validate(state["task"])
    quality_lines = [
        f"- {item['name']}: {'PASS' if item['ok'] else 'FAIL'}"
        for item in state.get("quality_results", [])
    ]
    risk_flags = state.get("risk_flags", [])
    risk_lines = [f"- {flag}" for flag in risk_flags] or ["- none detected"]
    return "\n".join(
        [
            "## Objective",
            f"{', '.join(task.requirement_ids)} — {task.objective}",
            "",
            "## Verification",
            *quality_lines,
            "- independent review: PASS",
            "",
            "## Risk",
            f"Task-declared level: {task.risk}",
            "Deterministic/declarative flags:",
            *risk_lines,
            "",
            "## Converge metadata",
            f"- run: {state['run_id']}",
            f"- task: {task.id}",
            f"- candidate_risk_sha256: {state.get('risk_fingerprint') or 'n/a'}",
            f"- requirements_sha256: {state['requirements_hash']}",
        ]
    )


def create_pr(state: WorkflowState) -> WorkflowState:
    cfg = load_config(state["config_path"])
    task = TaskEnvelope.model_validate(state["task"])
    adapter = GitHubAdapter(cfg)
    existing = state.get("pr")
    if existing:
        pr = adapter.get_pull_request(int(existing["number"]))
    else:
        pr = adapter.ensure_pull_request(
            head=state["branch"],
            base=cfg.base_branch,
            title=f"[Converge] {task.title}",
            body=_pull_request_body(state),
        )
    store = _evidence(state)
    store.write_json(state["run_id"], task.id, "pr.json", pr.model_dump(mode="json"))
    return {**state, "pr": pr.model_dump(mode="json"), "status": "pr_open"}


def ci_gate(state: WorkflowState) -> WorkflowState:
    cfg = load_config(state["config_path"])
    task = TaskEnvelope.model_validate(state["task"])
    pr = state["pr"]
    result = GitHubAdapter(cfg).wait_for_ci(pr["head_sha"])
    store = _evidence(state)
    store.write_json(state["run_id"], task.id, "ci.json", result.model_dump(mode="json"))
    return {**state, "ci": result.model_dump(mode="json"), "status": f"ci_{result.status}"}


def route_after_ci(state: WorkflowState) -> str:
    if state["ci"]["status"] == "pass":
        cfg = load_config(state["config_path"])
        return "merge" if cfg.auto_merge else "end"
    cfg = load_config(state["config_path"])
    if state.get("repair_attempts", 0) < cfg.max_repair_attempts:
        return "repair"
    if state.get("replan_attempts", 0) < cfg.max_replans:
        return "replan"
    return "human"


def merge_pr(state: WorkflowState) -> WorkflowState:
    cfg = load_config(state["config_path"])
    task = TaskEnvelope.model_validate(state["task"])
    merged_sha = GitHubAdapter(cfg).merge(int(state["pr"]["number"]))
    compliance = ComplianceSnapshot.model_validate(state["compliance"])
    compliance = ComplianceEngine.mark_remote_verified(
        compliance,
        task.requirement_ids,
        [f"github-pr:{state['pr']['url']}", f"merged-sha:{merged_sha}"],
    )
    store = _evidence(state)
    store.append_event(
        state["run_id"],
        "merged",
        {"task_id": task.id, "pr": state["pr"]["url"], "sha": merged_sha},
    )
    if state.get("branch"):
        delete_remote_branch(cfg.repo_path, state["branch"])
    if state.get("branch") and state.get("worktree"):
        cleanup_worktree(
            cfg.repo_path,
            Path(state["worktree"]),
            state["branch"],
        )
    next_state: WorkflowState = {
        **state,
        "compliance": compliance.model_dump(mode="json"),
        "status": "merged",
        "message": merged_sha,
    }
    _write_compliance(next_state, compliance)
    return next_state


def _mandatory_converged(state: WorkflowState) -> bool:
    requirements = {item["id"]: item for item in state["requirements"]}
    compliance = ComplianceSnapshot.model_validate(state["compliance"])
    mandatory_ids = {
        requirement_id
        for requirement_id, item in requirements.items()
        if item.get("severity", "mandatory") == "mandatory"
    }
    if not mandatory_ids:
        return True
    return all(
        compliance.entries.get(requirement_id) is not None
        and compliance.entries[requirement_id].status == RequirementStatus.PASS
        for requirement_id in mandatory_ids
    )


def refresh_from_main(state: WorkflowState) -> WorkflowState:
    cfg = load_config(state["config_path"])
    update_base(cfg.repo_path, cfg.base_branch)
    status = "ready_next_iteration"
    if _mandatory_converged(state):
        status = "converged"
    elif state.get("iteration", 0) >= cfg.max_iterations:
        status = "iteration_budget_exhausted"
    return {
        **state,
        "task": None,
        "worktree": None,
        "branch": None,
        "quality_results": [],
        "review_result": None,
        "commit_sha": None,
        "pr": None,
        "ci": None,
        "repair_attempts": 0,
        "replan_attempts": 0,
        "risk_flags": [],
        "approved_risk_flags": [],
        "risk_report": None,
        "risk_fingerprint": None,
        "status": status,
    }


def route_after_refresh(state: WorkflowState) -> str:
    if state.get("status") == "ready_next_iteration":
        return "continue"
    if state.get("status") == "iteration_budget_exhausted":
        return "human"
    return "end"


def build_graph(checkpointer=None):
    graph = StateGraph(WorkflowState)
    nodes = [
        ("bootstrap", bootstrap),
        ("guard_plan", guard_spec),
        ("guard_quality", guard_spec),
        ("spec_stop", spec_stop),
        ("pause_plan", pause_before_plan),
        ("pause_build", pause_before_build),
        ("pause_repair", pause_before_repair),
        ("pause_integrate", pause_before_integrate),
        ("pause_pr", pause_before_pr),
        ("pause_merge", pause_before_merge),
        ("plan", plan),
        ("prepare_worktree", prepare_worktree),
        ("build", build),
        ("quality", quality),
        ("review", review),
        ("repair", repair),
        ("replan", replan),
        ("human", human_gate),
        ("integrate", integrate),
        ("pr", create_pr),
        ("ci", ci_gate),
        ("merge", merge_pr),
        ("refresh", refresh_from_main),
    ]
    for name, node in nodes:
        graph.add_node(name, node)
    graph.add_edge(START, "bootstrap")
    graph.add_edge("bootstrap", "guard_plan")
    graph.add_conditional_edges(
        "guard_plan",
        route_after_guard,
        {"continue": "pause_plan", "end": END},
    )
    graph.add_conditional_edges(
        "pause_plan",
        route_after_pause,
        {"continue": "plan", "end": END},
    )
    graph.add_edge("plan", "prepare_worktree")
    graph.add_edge("prepare_worktree", "pause_build")
    graph.add_conditional_edges(
        "pause_build",
        route_after_pause,
        {"continue": "build", "end": END},
    )
    graph.add_edge("build", "guard_quality")
    graph.add_conditional_edges(
        "guard_quality",
        route_after_guard,
        {"continue": "quality", "end": END},
    )
    graph.add_edge("quality", "review")
    graph.add_conditional_edges(
        "review",
        route_after_review,
        {
            "integrate": "pause_integrate",
            "repair": "pause_repair",
            "replan": "replan",
            "human": "human",
            "spec_stop": "spec_stop",
        },
    )
    graph.add_edge("spec_stop", END)
    graph.add_conditional_edges(
        "pause_repair",
        route_after_pause,
        {"continue": "repair", "end": END},
    )
    graph.add_edge("repair", "guard_quality")
    graph.add_edge("replan", "guard_plan")
    graph.add_conditional_edges(
        "human",
        route_after_human,
        {
            "repair": "pause_repair",
            "prepare": "prepare_worktree",
            "plan": "guard_plan",
            "review": "guard_quality",
            "end": END,
        },
    )
    graph.add_conditional_edges(
        "pause_integrate",
        route_after_pause,
        {"continue": "integrate", "end": END},
    )
    graph.add_conditional_edges(
        "integrate",
        route_after_integrate,
        {"pr": "pause_pr", "end": END},
    )
    graph.add_conditional_edges(
        "pause_pr",
        route_after_pause,
        {"continue": "pr", "end": END},
    )
    graph.add_edge("pr", "ci")
    graph.add_conditional_edges(
        "ci",
        route_after_ci,
        {
            "merge": "pause_merge",
            "repair": "pause_repair",
            "replan": "replan",
            "human": "human",
            "end": END,
        },
    )
    graph.add_conditional_edges(
        "pause_merge",
        route_after_pause,
        {"continue": "merge", "end": END},
    )
    graph.add_edge("merge", "refresh")
    graph.add_conditional_edges(
        "refresh",
        route_after_refresh,
        {"continue": "guard_plan", "human": "human", "end": END},
    )
    return graph.compile(checkpointer=checkpointer)
