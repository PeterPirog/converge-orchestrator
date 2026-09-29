from __future__ import annotations

import argparse
import json
import os
import time
from pathlib import Path
from typing import Any
from unittest.mock import patch

from langgraph.checkpoint.base import BaseCheckpointSaver

from converge_orchestrator import runtime, runtime_service
from converge_orchestrator.runtime_service import ScheduledRunController

_global_state = {
    "sandbox_call_count": 0,
    "changed_files_call_count": 0,
    "builder_attempt": 0,
}


def _patched_sandbox_run(
    self,
    cmd: list[str],
    cwd: Path,
    timeout: int | None = None,
    env: dict | None = None,
    shell: bool = False,
    **kwargs,
):
    """Deterministic sandbox outputs: baseline passes, RED gate fails, rest pass."""
    _global_state["sandbox_call_count"] += 1
    if _global_state["sandbox_call_count"] == 1:
        return _completed(0, "1 passed")
    if _global_state["sandbox_call_count"] == 2:
        return _completed(1, "NEW_RULE_MISSING")
    return _completed(0, "1 passed")


def _completed(returncode: int, stdout: str):
    import types

    return types.SimpleNamespace(returncode=returncode, stdout=stdout)


def _patched_changed_files(worktree: Path, base_branch: str) -> list[str]:
    _global_state["changed_files_call_count"] += 1
    if _global_state["changed_files_call_count"] == 1:
        return []
    return ["tests/test_rule.py"]


def _patched_opencode_invoke(self, role: str, prompt: str, worktree: Path):
    import types

    if role == "scout":
        return types.SimpleNamespace(
            ok=True,
            output=json.dumps(
                {
                    "summary": "Test repository with Python code",
                    "stacks": ["python"],
                    "key_paths": ["src/"],
                    "test_paths": ["tests/"],
                    "architecture_notes": [],
                    "risk_notes": [],
                    "requirement_hints": {"ARCH-001": ["tests/"]},
                    "uncertainties": [],
                }
            ),
            context=None,
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
            context=None,
        )

    if role == "builder":
        _global_state["builder_attempt"] += 1
        print(
            f"DEBUG: builder attempt #{_global_state['builder_attempt']}", flush=True
        )
        if _global_state["builder_attempt"] >= 2:
            test_file = worktree / "tests" / "test_rule.py"
            if test_file.exists():
                test_file.write_bytes(b"def test_new_rule():\n    assert True\n")
            print("DEBUG: Crashing process (os._exit(94))", flush=True)
            os._exit(94)
        if _global_state["builder_attempt"] == 1:
            test_file = worktree / "tests" / "test_rule.py"
            test_file.parent.mkdir(parents=True, exist_ok=True)
            test_file.write_bytes(
                b"def test_new_rule():\n    assert False, 'NEW_RULE_MISSING'\n"
            )
            return types.SimpleNamespace(
                ok=True,
                output="Created RED test",
                context=None,
            )
        return types.SimpleNamespace(
            ok=True,
            output="Implementation complete",
            context=None,
        )

    if role == "reviewer":
        return types.SimpleNamespace(
            ok=True,
            output=json.dumps(
                {
                    "verdict": "pass",
                    "findings": [],
                    "reviewers": {
                        "correctness_reviewer": "pass",
                        "architecture_reviewer": "pass",
                    },
                }
            ),
            context=None,
        )

    return types.SimpleNamespace(ok=True, output="OK", context=None)


class BlobPutDelaySaver(BaseCheckpointSaver):
    """Delays the checkpoint blob put for the frozen-RED transition checkpoint.

    Widens exactly the persistence window that production hits when a process
    dies between the durable task writes and the asynchronous checkpoint blob
    put, deterministically producing the lost pending-trigger state.
    """

    def __init__(self, inner: Any, delay_seconds: float) -> None:
        super().__init__()
        self._inner = inner
        self._delay_seconds = delay_seconds

    def put(self, config: Any, checkpoint: Any, metadata: Any, new_versions: Any) -> Any:
        values = checkpoint.get("channel_values") or {}
        if values.get("status") == "frozen_red_snapshotted":
            print(
                "DEBUG: delaying checkpoint blob put for frozen_red_snapshotted",
                flush=True,
            )
            time.sleep(self._delay_seconds)
        return self._inner.put(config, checkpoint, metadata, new_versions)

    def put_writes(self, config: Any, writes: Any, task_id: str, task_path: str = "") -> Any:
        return self._inner.put_writes(config, writes, task_id, task_path)

    def get_tuple(self, config: Any) -> Any:
        return self._inner.get_tuple(config)

    def list(self, config: Any, **kwargs: Any) -> Any:
        return self._inner.list(config, **kwargs)

    def delete_thread(self, thread_id: str) -> None:
        self._inner.delete_thread(thread_id)


def _create_patches():
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


def _wrap_checkpointer_put(delay_seconds: float):
    from converge_orchestrator import persistence as persistence_module

    original = persistence_module.open_checkpointer

    def wrapped(state_dir, database_url=None):
        saver, db = original(state_dir, database_url)
        return BlobPutDelaySaver(saver, delay_seconds), db

    return patch.object(persistence_module, "open_checkpointer", wrapped)


def crash(registry_path: Path, config_path: Path) -> int:
    print("CRASH WORKER: Starting crash function", flush=True)
    try:
        runtime._LEASE_TTL_SECONDS = 1
        from converge_orchestrator.graph_service import build_graph

        runtime_service.build_graph = build_graph

        _global_state["sandbox_call_count"] = 0
        _global_state["changed_files_call_count"] = 0
        _global_state["builder_attempt"] = 0

        patcher1, patcher2, patcher3 = _create_patches()
        patcher4 = _wrap_checkpointer_put(1.5)
        for patcher in (patcher1, patcher2, patcher3, patcher4):
            patcher.start()
        print("CRASH WORKER: Patches applied", flush=True)

        try:
            controller = ScheduledRunController(registry_path, restore_on_start=False)
            controller.register_project("empty-next-chaos", config_path)
            controller.start_run("empty-next-chaos")

            deadline = time.monotonic() + 120
            while time.monotonic() < deadline:
                records = controller.registry.runs_for_project("empty-next-chaos")
                if records and records[0]["finished_at"]:
                    raise RuntimeError(
                        f"run finished instead of crashing: {records[0]}"
                    )
                time.sleep(0.5)
            raise RuntimeError("empty next chaos node did not terminate the process")
        finally:
            for patcher in (patcher1, patcher2, patcher3, patcher4):
                patcher.stop()
    except Exception as e:
        print(f"CRASH WORKER ERROR: {e}", flush=True)
        import traceback

        traceback.print_exc()
        return 1


def recover(registry_path: Path) -> int:
    print("RECOVER WORKER: Starting recover function", flush=True)
    try:
        runtime._LEASE_TTL_SECONDS = 1
        from converge_orchestrator.graph_service import build_graph

        runtime_service.build_graph = build_graph

        _global_state["sandbox_call_count"] = 0
        _global_state["changed_files_call_count"] = 0
        _global_state["builder_attempt"] = 0

        patcher1, patcher2, patcher3 = _create_patches()
        for patcher in (patcher1, patcher2, patcher3):
            patcher.start()
        print("RECOVER WORKER: Patches applied", flush=True)

        try:
            controller = ScheduledRunController(registry_path)
            deadline = time.monotonic() + 60
            last_record = None
            while time.monotonic() < deadline:
                records = controller.registry.runs_for_project("empty-next-chaos")
                if not records:
                    time.sleep(0.05)
                    continue
                last_record = records[0]
                print(
                    f"RECOVER WORKER: status={last_record['status']}, "
                    f"node={last_record.get('node')}, "
                    f"error={last_record.get('error')}, "
                    f"finished={last_record['finished_at']}",
                    flush=True,
                )
                if last_record["finished_at"]:
                    if last_record["status"] not in ("completed", "pushed"):
                        raise RuntimeError(f"recovered run failed: {last_record}")
                    print("RECOVER WORKER: Recovery successful", flush=True)
                    return 0
                time.sleep(0.05)

            raise RuntimeError(
                "recovered run did not reach a terminal checkpoint; "
                f"record={last_record!r}"
            )
        finally:
            for patcher in (patcher1, patcher2, patcher3):
                patcher.stop()
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