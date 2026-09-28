from __future__ import annotations

import argparse
import json
import os
import time
from pathlib import Path
from typing import Any
from unittest.mock import patch

from converge_orchestrator import runtime, runtime_service
from converge_orchestrator.runtime_service import ScheduledRunController

# Global state for crash control
_crash_on_builder = False
_builder_attempt = 0
_sandbox_call_count = 0
_changed_files_call_count = 0


def _make_completed(returncode: int, stdout: str):
    import types
    return types.SimpleNamespace(returncode=returncode, stdout=stdout)


def _patched_sandbox_run(
    self,
    cmd: list[str],
    cwd: Path,
    timeout: int | None = None,
    env: dict | None = None,
    shell: bool = False,
    **kwargs,
):
    """Monkeypatched ExecutionSandbox.run for deterministic test outputs."""
    global _sandbox_call_count
    _sandbox_call_count += 1
    print(f"DEBUG: _patched_sandbox_run called #{_sandbox_call_count}", flush=True)

    import types

    # First call is typically tdd_baseline (should pass)
    # Second call is typically tdd_red_gate (should fail with NEW_RULE_MISSING)
    # We can distinguish by checking if the test file exists and what it contains
    # But simpler: just alternate based on call count
    if _sandbox_call_count == 1:
        # Baseline - tests pass
        return types.SimpleNamespace(returncode=0, stdout="1 passed")
    elif _sandbox_call_count == 2:
        # RED gate - test fails with expected pattern
        return types.SimpleNamespace(returncode=1, stdout="NEW_RULE_MISSING")
    else:
        # Subsequent calls (e.g., tdd_green) - pass
        return types.SimpleNamespace(returncode=0, stdout="1 passed")


_changed_files_call_count = 0

def _patched_changed_files(worktree: Path, base_branch: str) -> list[str]:
    """Monkeypatched changed_files for TDD."""
    global _changed_files_call_count
    _changed_files_call_count += 1
    # First call (baseline) - no changes
    # Second call (red gate) - test file exists
    if _changed_files_call_count == 1:
        return []
    return ["tests/test_rule.py"]


def _patched_opencode_invoke(self, role: str, prompt: str, worktree: Path):
    """Monkeypatched OpenCodeAdapter.invoke for deterministic model outputs."""
    import types

    if role == "scout":
        return types.SimpleNamespace(
            ok=True,
            output=json.dumps({
                "summary": "Test repository with Python code",
                "stacks": ["python"],
                "key_paths": ["src/"],
                "test_paths": ["tests/"],
                "architecture_notes": [],
                "risk_notes": [],
                "requirement_hints": {"ARCH-001": ["tests/"]},
                "uncertainties": []
            }),
            context=None
        )

    if role == "planner":
        from converge_orchestrator.models import TaskEnvelope, TDDPlan
        task = TaskEnvelope(
            id="ARCH-001-1",
            requirement_ids=["ARCH-001"],
            title="Add behavior",
            objective="Expose the required behavior",
            allowed_paths=["**"],
            change_kind="behavior",
            tdd=TDDPlan(
                mode="required",
                test_paths=["tests/**"],
                test_gate="unit-test",
                expected_failure_pattern="NEW_RULE_MISSING",
                rationale="Observable behavior changes require a failing test first.",
            ),
        )
        return types.SimpleNamespace(
            ok=True,
            output=task.model_dump_json(),
            context=None
        )

    if role == "builder":
        global _builder_attempt
        _builder_attempt += 1
        print(f"DEBUG: builder attempt #{_builder_attempt}", flush=True)

        # First builder call is for tdd_red_build (creating RED test)
        # Second builder call is for build (implementation) - this is where we crash
        if _builder_attempt >= 2:
            # This is the production build node - mutate frozen RED and crash
            test_file = worktree / "tests" / "test_rule.py"
            if test_file.exists():
                test_file.write_bytes(b"def test_new_rule():\n    assert True\n")
            time.sleep(0.5)
            print("DEBUG: Crashing process (os._exit(94))", flush=True)
            os._exit(94)

        # For tdd_red_build (first builder call), create a RED test file
        if _builder_attempt == 1:
            test_file = worktree / "tests" / "test_rule.py"
            test_file.parent.mkdir(parents=True, exist_ok=True)
            test_file.write_bytes(b"def test_new_rule():\n    assert False, 'NEW_RULE_MISSING'\n")
            return types.SimpleNamespace(
                ok=True,
                output="Created RED test",
                context=None
            )

        # For normal build (when not crashing), return success
        return types.SimpleNamespace(
            ok=True,
            output="Implementation complete",
            context=None
        )

    if role == "reviewer":
        return types.SimpleNamespace(
            ok=True,
            output=json.dumps({
                "verdict": "pass",
                "findings": [],
                "reviewers": {"correctness_reviewer": "pass", "architecture_reviewer": "pass"}
            }),
            context=None
        )

    # Default fallback
    return types.SimpleNamespace(ok=True, output="OK", context=None)


def _create_patches():
    """Create patches for external dependencies."""
    patcher1 = patch(
        "converge_orchestrator.opencode.OpenCodeAdapter.invoke",
        _patched_opencode_invoke,
    )
    patcher2 = patch(
        "converge_orchestrator.sandbox.ExecutionSandbox.run",
        _patched_sandbox_run,
    )
    patcher3 = patch(
        "converge_orchestrator.tdd.changed_files",
        _patched_changed_files,
    )
    return patcher1, patcher2, patcher3


def crash(registry_path: Path, config_path: Path) -> int:
    print("CRASH WORKER: Starting crash function", flush=True)
    try:
        runtime._LEASE_TTL_SECONDS = 1
        # Use REAL graph_service.build_graph - do NOT override
        from converge_orchestrator.graph_service import build_graph
        runtime_service.build_graph = build_graph

        global _sandbox_call_count, _builder_attempt, _crash_on_builder, _changed_files_call_count
        _sandbox_call_count = 0
        _builder_attempt = 0
        _crash_on_builder = False
        _changed_files_call_count = 0

        patcher1, patcher2, patcher3 = _create_patches()
        patcher1.start()
        patcher2.start()
        patcher3.start()
        print("CRASH WORKER: Patches applied", flush=True)

        try:
            controller = ScheduledRunController(registry_path, restore_on_start=False)
            print("CRASH WORKER: Registering project", flush=True)
            controller.register_project("frozen-red-chaos", config_path)
            print("CRASH WORKER: Starting run", flush=True)
            controller.start_run("frozen-red-chaos")
            print("CRASH WORKER: Waiting for crash", flush=True)

            # Wait for the graph to reach the build node
            deadline = time.monotonic() + 120
            iteration = 0
            last_status = None
            last_node = None
            while time.monotonic() < deadline:
                iteration += 1
                records = controller.registry.runs_for_project("frozen-red-chaos")
                if records:
                    record = records[0]
                    status = record['status']
                    node = record.get('node')
                    if status != last_status or node != last_node:
                        print(
                            f"CRASH WORKER: Iteration {iteration}, "
                            f"status={status}, node={node}",
                            flush=True,
                        )
                        last_status = status
                        last_node = node
                    if record["finished_at"]:
                        raise RuntimeError(f"run finished instead of crashing: {record}")

                time.sleep(0.5)

            raise RuntimeError("frozen RED chaos node did not terminate the process")
        finally:
            patcher1.stop()
            patcher2.stop()
            patcher3.stop()
    except Exception as e:
        print(f"CRASH WORKER ERROR: {e}", flush=True)
        import traceback
        traceback.print_exc()
        return 1


def recover(registry_path: Path) -> int:
    print("RECOVER WORKER: Starting recover function", flush=True)
    try:
        runtime._LEASE_TTL_SECONDS = 1
        # Use REAL graph_service.build_graph - do NOT override
        from converge_orchestrator.graph_service import build_graph
        runtime_service.build_graph = build_graph

        # For recovery, we don't crash - let the builder complete normally
        global _crash_on_builder, _builder_attempt, _sandbox_call_count, _changed_files_call_count
        _crash_on_builder = False
        _builder_attempt = 0
        _sandbox_call_count = 0
        _changed_files_call_count = 0

        patcher1, patcher2, patcher3 = _create_patches()
        patcher1.start()
        patcher2.start()
        patcher3.start()
        print("RECOVER WORKER: Patches applied", flush=True)

        try:
            controller = ScheduledRunController(registry_path)
            deadline = time.monotonic() + 60
            last_record: dict[str, Any] | None = None
            last_signature: tuple[Any, ...] | None = None
            last_heartbeat = 0.0
            while time.monotonic() < deadline:
                records = controller.registry.runs_for_project("frozen-red-chaos")
                if not records:
                    time.sleep(0.05)
                    continue
                last_record = records[0]
                signature = (
                    last_record["status"],
                    last_record.get("node"),
                    last_record.get("error"),
                    last_record["finished_at"],
                )
                now = time.monotonic()
                if signature != last_signature or now - last_heartbeat >= 2.0:
                    print(
                        f"RECOVER WORKER: status={last_record['status']}, "
                        f"node={last_record.get('node')}, "
                        f"error={last_record.get('error')}, "
                        f"finished={last_record['finished_at']}",
                        flush=True,
                    )
                    last_signature = signature
                    last_heartbeat = now
                if last_record["finished_at"]:
                    if last_record["status"] not in ("completed", "pushed"):
                        raise RuntimeError(f"recovered run failed: {last_record}")
                    print("RECOVER WORKER: Recovery successful", flush=True)
                    return 0
                time.sleep(0.05)

            snapshot: dict[str, Any] | None = None
            if last_record is not None:
                snapshot = controller._snapshot(last_record)
            timer = controller._timers.get(last_record["id"] if last_record else "")
            diagnostics = {
                "record": last_record,
                "record_error": last_record.get("error") if last_record else None,
                "snapshot": snapshot,
                "timer_present": timer is not None,
                "timer_alive": bool(timer and timer.is_alive()),
                "timer_generation": (
                    controller._timer_generations.get(last_record["id"])
                    if last_record is not None
                    else None
                ),
                "worker_alive": bool(
                    last_record
                    and controller._workers.get(last_record["id"])
                    and controller._workers[last_record["id"]].is_alive()
                ),
            }
            raise RuntimeError(
                "recovered run did not reach a terminal checkpoint; "
                f"diagnostics={diagnostics!r}"
            )
        finally:
            patcher1.stop()
            patcher2.stop()
            patcher3.stop()
    except Exception as e:
        print(f"RECOVER WORKER ERROR: {e}", flush=True)
        import traceback
        traceback.print_exc()
        return 1


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("mode", choices=("crash", "recover"))
    parser.add_argument("registry_path", type=Path)
    parser.add_argument("config_path", nargs="?", type=Path)
    args = parser.parse_args()
    if args.mode == "crash":
        if args.config_path is None:
            parser.error("crash mode requires config_path")
        return crash(args.registry_path, args.config_path)
    return recover(args.registry_path)


if __name__ == "__main__":
    raise SystemExit(main())