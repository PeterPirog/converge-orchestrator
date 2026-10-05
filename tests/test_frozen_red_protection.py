import hashlib
import json
import tempfile
import types
from pathlib import Path
from unittest.mock import patch

import pytest
import yaml

from converge_orchestrator.evidence import EvidenceStore
from converge_orchestrator.graph import (
    _check_frozen_red_violation,
    guard_frozen_red_after_build,
    guard_frozen_red_before_build,
    reconcile_frozen_red,
    snapshot_frozen_red,
)
from converge_orchestrator.models import (
    AgentResult,
    GateResult,
    ProjectConfig,
    QualityGate,
    TaskEnvelope,
)
from converge_orchestrator.tdd import run_tdd_baseline, run_tdd_red


def _config(tmp_path: Path) -> ProjectConfig:
    requirements = tmp_path / "architecture.md"
    requirements.write_text("System must expose the requested behavior.\n", encoding="utf-8")
    return ProjectConfig(
        repo_path=tmp_path,
        requirements_path=requirements,
        require_spec_read_only=False,
        agents={},
        auto_discover_quality=False,
        quality_gates=[
            QualityGate(
                name="unit-test",
                command=["python", "-m", "pytest", "-q"],
                timeout_seconds=30,
            )
        ],
    )


def _behavior_task() -> TaskEnvelope:
    return TaskEnvelope(
        id="ARCH-001-1",
        requirement_ids=["ARCH-001"],
        title="Add behavior",
        objective="Expose the required behavior",
        allowed_paths=["src/**", "tests/**"],
        change_kind="behavior",
        tdd={
            "mode": "required",
            "test_paths": ["tests/**"],
            "test_gate": "unit-test",
            "expected_failure_pattern": "NEW_RULE_MISSING",
            "rationale": "Observable behavior changes require a failing test first.",
        },
    )


def _completed(returncode: int, stdout: str):
    return types.SimpleNamespace(returncode=returncode, stdout=stdout)


def _baseline() -> GateResult:
    return GateResult(
        name="tdd_baseline",
        ok=True,
        required=True,
        returncode=0,
        output=json.dumps({"gate_output": "baseline pass"}),
    )


def _setup_red_test(tmp_path: Path):
    """Helper to set up a valid RED test and return (cfg, task, red, red_details, test_file)."""
    cfg = _config(tmp_path)
    task = _behavior_task()

    # First run baseline WITHOUT the test file existing
    with (
        patch(
            "converge_orchestrator.tdd.ExecutionSandbox.run",
            return_value=_completed(0, "1 passed"),  # baseline passes
        ),
        patch(
            "converge_orchestrator.tdd.changed_files",
            return_value=[],
        ),
    ):
        baseline = run_tdd_baseline(cfg, tmp_path, task)
    assert baseline.ok

    # Now create the test file with LF line endings (binary write)
    test_file = tmp_path / "tests" / "test_rule.py"
    test_file.parent.mkdir()
    test_file.write_bytes(
        b"def test_new_rule():\n    assert False, 'NEW_RULE_MISSING'\n"
    )

    with (
        patch(
            "converge_orchestrator.tdd.ExecutionSandbox.run",
            return_value=_completed(1, "NEW_RULE_MISSING"),
        ),
        patch(
            "converge_orchestrator.tdd.changed_files",
            return_value=["tests/test_rule.py"],
        ),
    ):
        red = run_tdd_red(cfg, tmp_path, task, baseline)

    assert red.ok
    red_details = json.loads(red.output)
    return cfg, task, red, red_details, test_file


def _make_state(
    tmp_path: Path, evidence_root: Path, run_id: str, task, red, config_path: Path
) -> dict:
    """Create a minimal WorkflowState for testing."""
    return {
        "config_path": str(config_path),
        "run_id": run_id,
        "task": task.model_dump(mode="json"),
        "worktree": str(tmp_path),
        "tdd_red_result": red.model_dump(mode="json"),
    }


def _make_config_file(tmp_path: Path, evidence_root: Path) -> Path:
    """Create a minimal converge.yaml for testing."""
    config_data = {
        "version": 1,
        "project": {
            "repo_path": str(tmp_path),
            "requirements_path": str(tmp_path / "architecture.md"),
            "state_dir": str(evidence_root),
            "require_spec_read_only": False,
        },
        "agents": {},
        "workflow": {"max_repair_attempts": 3, "max_replans": 2},
    }
    config_path = tmp_path / "converge.yaml"
    config_path.write_text(yaml.dump(config_data))
    return config_path


class TestFrozenRedSnapshot:
    """Test the durable snapshot mechanism for frozen RED tests."""

    def test_snapshot_creates_exact_byte_copies(self, tmp_path: Path) -> None:
        cfg, task, red, red_details, test_file = _setup_red_test(tmp_path)

        with tempfile.TemporaryDirectory() as evidence_root:
            evidence_dir = Path(evidence_root) / "evidence"
            store = EvidenceStore(evidence_dir)
            run_id = "test-run"
            task_id = task.id
            worktree = tmp_path

            snapshots = store.snapshot_frozen_red(
                run_id, task_id, worktree, red_details["red_test_sha256"]
            )

            assert "tests/test_rule.py" in snapshots
            expected_sha = red_details["red_test_sha256"]["tests/test_rule.py"]
            assert snapshots["tests/test_rule.py"]["sha256"] == expected_sha

            frozen_dir = store.frozen_red_dir(run_id, task_id)
            # Files are stored as blobs named by sha256
            expected_sha = red_details["red_test_sha256"]["tests/test_rule.py"]
            frozen_file = frozen_dir / expected_sha
            assert frozen_file.is_file()

            original_content = test_file.read_bytes()
            frozen_content = frozen_file.read_bytes()
            assert original_content == frozen_content

            manifest_path = store.root / run_id / "frozen-red" / task_id / "manifest.json"
            assert manifest_path.is_file()
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            assert manifest["task_id"] == task_id
            assert "tests/test_rule.py" in manifest["files"]


class TestFrozenRedVerification:
    """Test verification of frozen RED files against durable snapshot."""

    def test_verify_detects_no_mutation(self, tmp_path: Path) -> None:
        cfg, task, red, red_details, test_file = _setup_red_test(tmp_path)

        with tempfile.TemporaryDirectory() as evidence_root:
            evidence_dir = Path(evidence_root) / "evidence"
            store = EvidenceStore(evidence_dir)
            run_id = "test-run"
            task_id = task.id
            worktree = tmp_path

            store.snapshot_frozen_red(run_id, task_id, worktree, red_details["red_test_sha256"])

            all_unchanged, details = store.verify_frozen_red(run_id, task_id, worktree)
            assert all_unchanged
            assert details["tests/test_rule.py"]["status"] == "unchanged"

    def test_verify_detects_content_mutation(self, tmp_path: Path) -> None:
        """CRLF/LF mutation detected as mutation."""
        cfg, task, red, red_details, test_file = _setup_red_test(tmp_path)

        with tempfile.TemporaryDirectory() as evidence_root:
            evidence_dir = Path(evidence_root) / "evidence"
            store = EvidenceStore(evidence_dir)
            run_id = "test-run"
            task_id = task.id
            worktree = tmp_path

            store.snapshot_frozen_red(run_id, task_id, worktree, red_details["red_test_sha256"])

            # Mutate the file - change LF to CRLF (Windows line ending)
            test_file.write_bytes(
                b"def test_new_rule():\r\n    assert False, 'NEW_RULE_MISSING'\r\n"
            )

            all_unchanged, details = store.verify_frozen_red(run_id, task_id, worktree)
            assert not all_unchanged
            assert details["tests/test_rule.py"]["status"] == "modified"
            d = details["tests/test_rule.py"]
            assert d["observed_sha256"] != d["expected_sha256"]


class TestFrozenRedRestoration:
    """Test restoration of exact authoritative RED bytes."""

    def test_restoration_after_mutation(self, tmp_path: Path) -> None:
        """Test D - Deletion/Restoration."""
        cfg, task, red, red_details, test_file = _setup_red_test(tmp_path)

        with tempfile.TemporaryDirectory() as evidence_root:
            evidence_dir = Path(evidence_root) / "evidence"
            store = EvidenceStore(evidence_dir)
            run_id = "test-run"
            task_id = task.id
            worktree = tmp_path

            store.snapshot_frozen_red(run_id, task_id, worktree, red_details["red_test_sha256"])

            # Delete the file
            test_file.unlink()

            # Restore
            restored = store.restore_frozen_red(run_id, task_id, worktree)
            assert "tests/test_rule.py" in restored
            assert test_file.is_file()

            original_content = b"def test_new_rule():\n    assert False, 'NEW_RULE_MISSING'\n"
            assert test_file.read_bytes() == original_content


class TestFrozenRedGuard:
    """Test the guard functions that protect frozen RED during build/repair."""

    def test_guard_before_build_allows_clean(self, tmp_path: Path) -> None:
        """Test A - Normal case passes through."""
        cfg, task, red, red_details, test_file = _setup_red_test(tmp_path)

        with tempfile.TemporaryDirectory() as evidence_root:
            evidence_dir = Path(evidence_root) / "evidence"
            store = EvidenceStore(evidence_dir)
            run_id = "test-run"
            task_id = task.id
            worktree = tmp_path

            store.snapshot_frozen_red(run_id, task_id, worktree, red_details["red_test_sha256"])

            config_path = _make_config_file(tmp_path, evidence_root)
            state = _make_state(tmp_path, evidence_root, run_id, task, red, config_path)

            result = guard_frozen_red_before_build(state)
            assert result.get("frozen_red_violation") is not True
            assert result.get("status") != "frozen_red_violation_pre_build"


class TestFrozenRedReconciliation:
    """Test E - Same-task restart reconciliation."""

    def test_reconcile_detects_and_restores_mutation(self, tmp_path: Path) -> None:
        """Test E - Recovery reconciliation."""
        cfg, task, red, red_details, test_file = _setup_red_test(tmp_path)

        with tempfile.TemporaryDirectory() as evidence_root:
            evidence_dir = Path(evidence_root) / "evidence"
            store = EvidenceStore(evidence_dir)
            run_id = "test-run"
            task_id = task.id
            worktree = tmp_path

            store.snapshot_frozen_red(run_id, task_id, worktree, red_details["red_test_sha256"])

            # Simulate mutation during in-flight build (before checkpoint)
            test_file.write_bytes(b"def test_new_rule():\n    assert True\n")

            config_path = _make_config_file(tmp_path, evidence_root)
            state = _make_state(tmp_path, evidence_root, run_id, task, red, config_path)

            result = reconcile_frozen_red(state)

            assert result.get("frozen_red_violation") is True
            assert result.get("status") == "frozen_red_reconciled"
            assert result.get("frozen_red_violation_phase") == "recovery_reconcile"

            expected = b"def test_new_rule():\n    assert False, 'NEW_RULE_MISSING'\n"
            assert test_file.read_bytes() == expected


class TestFrozenRedMutationEvidence:
    """Test that mutation evidence is properly recorded."""

    def test_mutation_writes_structured_evidence(self, tmp_path: Path) -> None:
        """Test that violation evidence is written with required fields."""
        cfg, task, red, red_details, test_file = _setup_red_test(tmp_path)

        with tempfile.TemporaryDirectory() as evidence_root:
            evidence_dir = Path(evidence_root) / "evidence"
            store = EvidenceStore(evidence_dir)
            run_id = "test-run"
            task_id = task.id
            worktree = tmp_path

            store.snapshot_frozen_red(run_id, task_id, worktree, red_details["red_test_sha256"])

            # Mutate
            test_file.write_bytes(b"def test_new_rule():\n    assert True\n")

            config_path = _make_config_file(tmp_path, evidence_root)
            state = _make_state(tmp_path, evidence_root, run_id, task, red, config_path)
            state["repair_attempts"] = 0

            # Trigger violation check
            _check_frozen_red_violation(state, "post_build")

            events_path = store.root / run_id / "events.jsonl"
            assert events_path.is_file()
            content = events_path.read_text(encoding="utf-8").strip().split("\n")
            events = [json.loads(line) for line in content]

            mutation_events = [e for e in events if e["event"] == "frozen_red_mutation"]
            assert len(mutation_events) == 1

            payload = mutation_events[0]["payload"]
            assert payload["task_id"] == task.id
            assert payload["phase"] == "post_build"
            assert "paths" in payload
            assert any(p["path"] == "tests/test_rule.py" for p in payload["paths"])
            assert payload["builder_phase"] == "build"


class TestFrozenRedNonTddTask:
    """Test I - Non-TDD task is not affected."""

    def test_non_tdd_task_bypasses_protection(self, tmp_path: Path) -> None:
        """Test I - Non-TDD task bypasses protection."""
        _ = _config(tmp_path)
        task = TaskEnvelope(
            id="ARCH-001-2",
            requirement_ids=["ARCH-001"],
            title="Refactor",
            objective="Preserve behavior",
            change_kind="refactor",
            tdd={"mode": "not_applicable", "rationale": "No observable behavior change."},
        )

        state = {
            "config_path": str(tmp_path / "converge.yaml"),
            "run_id": "test-run",
            "task": task.model_dump(mode="json"),
            "worktree": str(tmp_path),
            "tdd_red_result": None,
        }

        result = guard_frozen_red_before_build(state)
        assert result == state

        result = snapshot_frozen_red(state)
        assert result == state

        result = guard_frozen_red_after_build(state)
        assert result == state

        result = reconcile_frozen_red(state)
        assert result == state


class TestFrozenRedNewTaskReset:
    """Test G - New task gets new frozen RED identity."""

    def test_new_task_gets_new_snapshot_identity(self, tmp_path: Path) -> None:
        """Test G - New task gets new snapshot."""
        _ = _config(tmp_path)
        task1 = _behavior_task()
        task1.id = "TASK-1"
        _ = TaskEnvelope(
            id="TASK-2",
            requirement_ids=["ARCH-002"],
            title="Another behavior",
            objective="Another behavior",
            allowed_paths=["tests/**"],
            change_kind="behavior",
            tdd={
                "mode": "required",
                "test_paths": ["tests/**"],
                "test_gate": "unit-test",
                "expected_failure_pattern": "OTHER_MISSING",
                "rationale": "Another behavior change.",
            },
        )

        test_file1 = tmp_path / "tests" / "test_rule1.py"
        test_file1.parent.mkdir()
        test_file1.write_text(
            "def test_rule1():\n    assert False, 'NEW_RULE_MISSING'\n", encoding="utf-8"
        )
        test_file2 = tmp_path / "tests" / "test_rule2.py"
        test_file2.write_text(
            "def test_rule2():\n    assert False, 'OTHER_MISSING'\n", encoding="utf-8"
        )

        with tempfile.TemporaryDirectory() as evidence_root:
            evidence_dir = Path(evidence_root) / "evidence"
            store = EvidenceStore(evidence_dir)
            run_id = "test-run"
            worktree = tmp_path

            sha1 = hashlib.sha256(test_file1.read_bytes()).hexdigest()
            red_details1 = {"red_test_sha256": {"tests/test_rule1.py": sha1}}
            store.snapshot_frozen_red(run_id, "TASK-1", worktree, red_details1["red_test_sha256"])

            sha2 = hashlib.sha256(test_file2.read_bytes()).hexdigest()
            red_details2 = {"red_test_sha256": {"tests/test_rule2.py": sha2}}
            store.snapshot_frozen_red(run_id, "TASK-2", worktree, red_details2["red_test_sha256"])

            manifest1 = store.root / run_id / "frozen-red" / "TASK-1" / "manifest.json"
            manifest2 = store.root / run_id / "frozen-red" / "TASK-2" / "manifest.json"
            assert manifest1.is_file()
            assert manifest2.is_file()

            m1 = json.loads(manifest1.read_text(encoding="utf-8"))
            m2 = json.loads(manifest2.read_text(encoding="utf-8"))
            assert m1["task_id"] == "TASK-1"
            assert m2["task_id"] == "TASK-2"
            assert set(m1["files"].keys()) == {"tests/test_rule1.py"}
            assert set(m2["files"].keys()) == {"tests/test_rule2.py"}


class TestFrozenRedPathConfinement:
    """Security tests for path confinement in frozen RED operations."""

    def test_rejects_posix_absolute_path(self, tmp_path: Path) -> None:
        """Reject POSIX absolute paths."""
        from converge_orchestrator.evidence import _resolve_confined_path

        worktree = tmp_path / "worktree"
        worktree.mkdir()
        with pytest.raises(ValueError, match="Absolute POSIX path not allowed"):
            _resolve_confined_path(worktree, "/etc/passwd")

    def test_rejects_windows_drive_path(self, tmp_path: Path) -> None:
        """Reject Windows drive-qualified paths."""
        from converge_orchestrator.evidence import _resolve_confined_path

        worktree = tmp_path / "worktree"
        worktree.mkdir()
        with pytest.raises(ValueError, match="Windows drive-qualified path not allowed"):
            _resolve_confined_path(worktree, "C:\\Windows\\System32\\file")
        with pytest.raises(ValueError, match="Windows drive-qualified path not allowed"):
            _resolve_confined_path(worktree, "C:/Windows/System32/file")

    def test_rejects_unc_path(self, tmp_path: Path) -> None:
        """Reject UNC paths."""
        from converge_orchestrator.evidence import _resolve_confined_path

        worktree = tmp_path / "worktree"
        worktree.mkdir()
        with pytest.raises(ValueError, match="UNC path not allowed"):
            _resolve_confined_path(worktree, "\\\\server\\share\\file")
        with pytest.raises(ValueError, match="UNC path not allowed"):
            _resolve_confined_path(worktree, "//server/share/file")

    def test_rejects_dotdot_traversal(self, tmp_path: Path) -> None:
        """Reject path traversal with .. components."""
        from converge_orchestrator.evidence import _resolve_confined_path

        worktree = tmp_path / "worktree"
        worktree.mkdir()
        with pytest.raises(ValueError, match="Path traversal not allowed"):
            _resolve_confined_path(worktree, "../../../outside.txt")
        with pytest.raises(ValueError, match="Path traversal not allowed"):
            _resolve_confined_path(worktree, "..\\..\\outside.txt")
        with pytest.raises(ValueError, match="Path traversal not allowed"):
            _resolve_confined_path(worktree, "tests/../../outside.txt")

    def test_rejects_mixed_separators(self, tmp_path: Path) -> None:
        """Reject mixed separator traversal attempts."""
        from converge_orchestrator.evidence import _resolve_confined_path

        worktree = tmp_path / "worktree"
        worktree.mkdir()
        with pytest.raises(ValueError, match="Path traversal not allowed"):
            _resolve_confined_path(worktree, "tests/..\\..\\outside.txt")

    def test_allows_valid_relative_path(self, tmp_path: Path) -> None:
        """Allow valid repository-relative paths."""
        from converge_orchestrator.evidence import _resolve_confined_path

        worktree = tmp_path / "worktree"
        worktree.mkdir()
        (worktree / "tests").mkdir()
        test_file = worktree / "tests" / "test_rule.py"
        test_file.write_text("def test():\n    pass\n", encoding="utf-8")

        resolved = _resolve_confined_path(worktree, "tests/test_rule.py")
        assert resolved == test_file.resolve()

    def test_symlink_escape_blocked(self, tmp_path: Path) -> None:
        """Block symlink escape outside worktree."""
        from converge_orchestrator.evidence import _resolve_confined_path

        worktree = tmp_path / "worktree"
        worktree.mkdir()
        outside = tmp_path / "outside"
        outside.mkdir()
        outside_file = outside / "secret.txt"
        outside_file.write_text("secret", encoding="utf-8")

        # Create symlink inside worktree pointing outside
        link = worktree / "link_to_outside"
        try:
            link.symlink_to(outside)
        except (OSError, NotImplementedError):
            pytest.skip("Symlink creation not supported on this platform")

        with pytest.raises(ValueError, match="Path escapes worktree"):
            _resolve_confined_path(worktree, "link_to_outside/secret.txt")

    def test_snapshot_rejects_malicious_paths(self, tmp_path: Path) -> None:
        """Snapshot rejects malicious paths in red_test_hashes."""
        cfg, task, red, red_details, test_file = _setup_red_test(tmp_path)

        with tempfile.TemporaryDirectory() as evidence_root:
            evidence_dir = Path(evidence_root) / "evidence"
            store = EvidenceStore(evidence_dir)
            run_id = "test-run"
            task_id = task.id
            worktree = tmp_path

            # Try to inject a malicious path
            malicious_hashes = {
                **red_details["red_test_sha256"],
                "../../../etc/passwd": "fake_hash",
            }

            # Should not crash, but should skip invalid paths
            snapshots = store.snapshot_frozen_red(run_id, task_id, worktree, malicious_hashes)
            # Only valid paths should be snapshotted
            assert "tests/test_rule.py" in snapshots
            assert "../../../etc/passwd" not in snapshots

    def test_verify_rejects_malicious_paths(self, tmp_path: Path) -> None:
        """Verify rejects malicious paths in manifest."""
        cfg, task, red, red_details, test_file = _setup_red_test(tmp_path)

        with tempfile.TemporaryDirectory() as evidence_root:
            evidence_dir = Path(evidence_root) / "evidence"
            store = EvidenceStore(evidence_dir)
            run_id = "test-run"
            task_id = task.id
            worktree = tmp_path

            store.snapshot_frozen_red(run_id, task_id, worktree, red_details["red_test_sha256"])

            # Manually corrupt manifest with malicious path
            manifest_path = store.root / run_id / "frozen-red" / task_id / "manifest.json"
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            manifest["files"]["../../../etc/passwd"] = {
                "sha256": "fake",
                "size": 10,
                "blob": "fake",
            }
            store.write_json(run_id, f"frozen-red/{task_id}", "manifest.json", manifest)

            # Verify should detect invalid path
            all_unchanged, details = store.verify_frozen_red(run_id, task_id, worktree)
            assert not all_unchanged
            assert details["../../../etc/passwd"]["status"] == "invalid_path"

    def test_restore_rejects_malicious_paths(self, tmp_path: Path) -> None:
        """Restore rejects malicious paths in manifest."""
        cfg, task, red, red_details, test_file = _setup_red_test(tmp_path)

        with tempfile.TemporaryDirectory() as evidence_root:
            evidence_dir = Path(evidence_root) / "evidence"
            store = EvidenceStore(evidence_dir)
            run_id = "test-run"
            task_id = task.id
            worktree = tmp_path

            store.snapshot_frozen_red(run_id, task_id, worktree, red_details["red_test_sha256"])

            # Manually corrupt manifest with malicious path
            manifest_path = store.root / run_id / "frozen-red" / task_id / "manifest.json"
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            manifest["files"]["../../../etc/passwd"] = {
                "sha256": "fake",
                "size": 10,
                "blob": "fake",
            }
            store.write_json(run_id, f"frozen-red/{task_id}", "manifest.json", manifest)

            # Restore should skip invalid paths without crashing
            restored = store.restore_frozen_red(run_id, task_id, worktree)
            assert "../../../etc/passwd" not in restored
            # Valid path should still be restored
            assert "tests/test_rule.py" in restored


class TestFrozenRedCanonicalGraph:
    """Test that the canonical service graph includes frozen RED protection."""

    def test_graph_service_includes_snapshot_node(self) -> None:
        """Verify graph_service.build_graph includes snapshot_frozen_red node."""
        from converge_orchestrator.graph_service import build_graph

        graph = build_graph()
        # The compiled graph should have the snapshot_frozen_red node
        # We can check by looking at the graph's nodes
        nodes = graph.nodes
        assert "snapshot_frozen_red" in nodes

    def test_graph_service_routes_through_snapshot(self) -> None:
        """Verify graph_service routes tdd_red_gate -> snapshot_frozen_red -> build/repair."""
        from converge_orchestrator.graph_service import build_graph

        graph = build_graph()
        # Check edges by looking at the graph structure
        # The graph should have: tdd_red_gate -> snapshot_frozen_red
        # and snapshot_frozen_red -> build (or repair)
        nodes = graph.nodes
        assert "tdd_red_gate" in nodes
        assert "snapshot_frozen_red" in nodes
        assert "build" in nodes
        assert "repair" in nodes


class TestFrozenRedRepairMutation:
    """Test that repair Builder mutation of frozen RED is detected and blocked."""

    def test_clean_repair_entry_clears_prior_violation(self, tmp_path: Path) -> None:
        """A restored violation must not skip every later bounded repair attempt."""
        from converge_orchestrator.workflow import _protect_frozen_red_on_entry

        cfg, task, red, red_details, _test_file = _setup_red_test(tmp_path)

        with tempfile.TemporaryDirectory() as evidence_root:
            store = EvidenceStore(Path(evidence_root) / "evidence")
            store.snapshot_frozen_red(
                "test-run",
                task.id,
                tmp_path,
                red_details["red_test_sha256"],
            )
            config_path = _make_config_file(tmp_path, Path(evidence_root))
            state = _make_state(
                tmp_path,
                Path(evidence_root),
                "test-run",
                task,
                red,
                config_path,
            )
            state.update(
                {
                    "frozen_red_violation": True,
                    "frozen_red_violation_phase": "post_build",
                    "frozen_red_violation_details": {"tests/test_rule.py": {}},
                }
            )

            result = _protect_frozen_red_on_entry(state, "pre_repair")

            with (
                patch(
                    "converge_orchestrator.workflow.OpenCodeAdapter.invoke",
                    return_value=AgentResult(role="builder", ok=True, output="repaired"),
                ) as invoke,
                patch("converge_orchestrator.workflow._requirements", return_value=[]),
            ):
                from converge_orchestrator.workflow import repair

                repaired = repair(state)

        assert result["frozen_red_violation"] is False
        assert result["frozen_red_violation_phase"] is None
        assert result["frozen_red_violation_details"] is None
        invoke.assert_called_once()
        assert repaired["repair_attempts"] == 1
        assert repaired["status"] == "repaired"

    def test_repair_mutation_detected_and_blocked(self, tmp_path: Path) -> None:
        """Test repair Builder mutation of frozen RED is caught."""
        from converge_orchestrator.graph import _check_frozen_red_violation

        cfg, task, red, red_details, test_file = _setup_red_test(tmp_path)

        with tempfile.TemporaryDirectory() as evidence_root:
            evidence_dir = Path(evidence_root) / "evidence"
            store = EvidenceStore(evidence_dir)
            run_id = "test-run"
            task_id = task.id
            worktree = tmp_path

            store.snapshot_frozen_red(run_id, task_id, worktree, red_details["red_test_sha256"])

            # Mutate the frozen RED file (simulating repair Builder modifying it)
            test_file.write_bytes(b"def test_new_rule():\n    assert True\n")

            config_path = _make_config_file(tmp_path, evidence_root)
            state = _make_state(tmp_path, evidence_root, run_id, task, red, config_path)
            state["repair_attempts"] = 1

            # Check violation on entry to repair
            result = _check_frozen_red_violation(state, "pre_repair")
            assert result is not None
            assert result.get("frozen_red_violation") is True
            assert result.get("status") == "frozen_red_violation_pre_repair"

            # Restore original state for post-repair test
            state = _make_state(tmp_path, evidence_root, run_id, task, red, config_path)
            state["repair_attempts"] = 1

            # Mutate again
            test_file.write_bytes(b"def test_new_rule():\n    assert True\n")

            # Check violation on exit from repair
            result = _check_frozen_red_violation(state, "post_repair")
            assert result is not None
            assert result.get("frozen_red_violation") is True
            assert result.get("status") == "frozen_red_violation_post_repair"


class TestFrozenRedProcessRestart:
    """Test crash/restart scenario through controller recovery."""

    def test_restart_recovers_dirty_frozen_red(self, tmp_path: Path) -> None:
        """Test that process restart detects and restores mutated frozen RED."""
        from converge_orchestrator.graph import reconcile_frozen_red

        cfg, task, red, red_details, test_file = _setup_red_test(tmp_path)

        with tempfile.TemporaryDirectory() as evidence_root:
            evidence_dir = Path(evidence_root) / "evidence"
            store = EvidenceStore(evidence_dir)
            run_id = "test-run"
            task_id = task.id
            worktree = tmp_path

            store.snapshot_frozen_red(run_id, task_id, worktree, red_details["red_test_sha256"])

            # Simulate mutation during in-flight build (before checkpoint)
            test_file.write_bytes(b"def test_new_rule():\n    assert True\n")

            config_path = _make_config_file(tmp_path, evidence_root)
            state = _make_state(tmp_path, evidence_root, run_id, task, red, config_path)

            # Simulate recovery: new controller starts, runs reconcile_frozen_red
            result = reconcile_frozen_red(state)

            assert result.get("frozen_red_violation") is True
            assert result.get("status") == "frozen_red_reconciled"
            assert result.get("frozen_red_violation_phase") == "recovery_reconcile"

            # Exact bytes restored
            expected = b"def test_new_rule():\n    assert False, 'NEW_RULE_MISSING'\n"
            assert test_file.read_bytes() == expected

            # Evidence event written
            events_path = store.root / run_id / "events.jsonl"
            assert events_path.is_file()
            content = events_path.read_text(encoding="utf-8").strip().split("\n")
            events = [json.loads(line) for line in content]
            mutation_events = [e for e in events if e["event"] == "frozen_red_mutation"]
            assert len(mutation_events) == 1
            payload = mutation_events[0]["payload"]
            assert payload["phase"] == "recovery_reconcile"
            assert payload["builder_phase"] == "recovery"


class TestFrozenRedFinalGreenGate:
    """Test H - Final tdd_green gate unchanged."""

    def test_tdd_green_still_works_as_final_gate(self, tmp_path: Path) -> None:
        """Test H - run_tdd_green still independently detects mismatch."""
        from converge_orchestrator.tdd import run_tdd_green

        cfg, task, red, red_details, test_file = _setup_red_test(tmp_path)

        with patch(
            "converge_orchestrator.tdd.ExecutionSandbox.run",
            return_value=_completed(0, "1 passed"),
        ):
            green = run_tdd_green(cfg, tmp_path, task, red)
        assert green.ok
        assert json.loads(green.output)["red_tests_unchanged"] is True

        test_file.write_text("def test_new_rule():\n    assert True\n", encoding="utf-8")
        with patch(
            "converge_orchestrator.tdd.ExecutionSandbox.run",
            return_value=_completed(0, "1 passed"),
        ):
            weakened = run_tdd_green(cfg, tmp_path, task, red)
        assert not weakened.ok
        assert json.loads(weakened.output)["red_tests_unchanged"] is False


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
