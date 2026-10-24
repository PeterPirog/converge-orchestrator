"""REQ-CONFIG-004: explicit quality gates and requirement-verifier bindings live in project
configuration, never in the Source of Truth.

REQ-CONFIG-004 (CONVERGE_ORCHESTRATOR_2_0_SOURCE_OF_TRUTH.md:L202
[6. Project configuration and secret sources]): project configuration SHALL permit explicit
deterministic quality gates and requirement-specific verifier bindings without modifying the
Source of Truth.

This deterministic offline regression locks the invariant on the canonical configuration path:
a non-empty ``quality.gates`` entry and a ``quality.requirement_verifiers`` binding declared
only in converge.yaml are loaded by ``load_config``, the explicit gate executes its configured
command, and the bound requirements are enforced to PASS (configured command succeeds) and to
FAIL (configured command does not) by their deterministic verifiers, while the Source of Truth
the compiler read stays byte-identical in content and sha256 digest across configuration
loading and verifier execution. The verifier bindings are keyed by the explicit requirement
IDs compiled from that Source of Truth, never by generated IDs. Only pytest tmp_path and
``sys.executable`` subprocesses are used; no network, model, git, or external tool access
occurs.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path
from typing import Any

import yaml

from converge_orchestrator.config import load_config
from converge_orchestrator.quality import required_gates_pass, run_quality_gates
from converge_orchestrator.spec import compile_contract, sha256_file
from converge_orchestrator.verification import run_requirement_verifiers

# The fixture Source of Truth declares the REQ-CONFIG-004 requirement itself, plus one explicit
# requirement dedicated to the enforced FAIL path.
_PASS_STATEMENT = (
    "REQ-CONFIG-004 Project configuration SHALL permit explicit deterministic quality "
    "gates and requirement-specific verifier bindings without modifying the Source of Truth."
)
_FAIL_STATEMENT = (
    "REQ-CONFIG-005 Deterministic verifiers SHALL report PASS or FAIL for bound "
    "requirements without model access."
)
_SOT_TEXT = (
    "# Converge Orchestrator 2.0 Source of Truth\n"
    "\n"
    "## 6. Project configuration and secret sources\n"
    "\n"
    f"{_PASS_STATEMENT}\n"
    f"{_FAIL_STATEMENT}\n"
)

# Deterministic ``sys.executable -c`` scripts; each runs entirely inside the repository cwd.
_GATE_MARKER = "explicit-quality-gate-executed"
_GATE_SCRIPT = (
    "from pathlib import Path; "
    f"Path('gate-executed.txt').write_text({_GATE_MARKER!r}, encoding='utf-8'); "
    f"print({_GATE_MARKER!r})"
)
_PASS_SCRIPT = (
    "from pathlib import Path; "
    "value = Path('gate-executed.txt').read_text(encoding='utf-8').strip(); "
    f"raise SystemExit(0 if value == {_GATE_MARKER!r} else 1)"
)
_FAIL_SCRIPT = (
    "from pathlib import Path; "
    "raw = Path('broken-artifact.txt'); "
    "value = raw.read_text(encoding='utf-8').strip() if raw.is_file() else ''; "
    "raise SystemExit(0 if value == 'ok' else 1)"
)

# Generated stable IDs are ``REQ-<10 uppercase hex>``; bindings must never use them.
_GENERATED_ID = re.compile(r"^REQ-[0-9A-F]{10}$")


def _config_data(pass_id: str, fail_id: str) -> dict[str, Any]:
    """One canonical converge.yaml whose quality policy is declared only outside the SOT.

    The non-empty ``quality.gates`` entry and the ``quality.requirement_verifiers`` binding
    are the sole declarations of the executed commands; the Source of Truth carries none.
    """
    return {
        "version": 1,
        "project": {
            "name": "req-config-004-fixture",
            "repo_path": "./repository",
            "requirements_path": "./source_of_truth.md",
            "state_dir": "./.state",
            "worktree_dir": "./.worktrees",
            "require_spec_read_only": False,
        },
        "sandbox": {"mode": "host"},
        "agents": {},
        "quality": {
            "auto_discover": False,
            "gates": [
                {
                    "name": "explicit-configured-check",
                    "command": [sys.executable, "-c", _GATE_SCRIPT],
                    "required": True,
                    "timeout_seconds": 30,
                },
            ],
            "requirement_verifiers": {
                pass_id: [
                    {
                        "name": "explicit-gate-executed",
                        "command": [sys.executable, "-c", _PASS_SCRIPT],
                        "required": True,
                        "timeout_seconds": 30,
                    }
                ],
                fail_id: [
                    {
                        "name": "deterministic-failure",
                        "command": [sys.executable, "-c", _FAIL_SCRIPT],
                        "required": True,
                        "timeout_seconds": 30,
                    }
                ],
            },
        },
    }


def test_explicit_quality_bindings_in_converge_yaml_execute_and_preserve_source_of_truth(
    tmp_path: Path,
) -> None:
    project = tmp_path / "project"
    repository = project / "repository"
    repository.mkdir(parents=True)
    # Deterministic FAIL input: an artifact whose content fails its bound verifier.
    (repository / "broken-artifact.txt").write_text("broken", encoding="utf-8")
    sot_path = project / "source_of_truth.md"
    sot_path.write_text(_SOT_TEXT, encoding="utf-8")

    source_bytes_before = sot_path.read_bytes()
    source_digest_before = sha256_file(sot_path)

    # Compile the fixture Source of Truth first: verifier bindings must be keyed by the
    # explicit IDs the compiler assigned, not by generated ones.
    contract = compile_contract(sot_path)
    by_statement = {record.statement: record for record in contract.requirements}
    pass_record = by_statement[_PASS_STATEMENT]
    fail_record = by_statement[_FAIL_STATEMENT]
    assert pass_record.id == "REQ-CONFIG-004"
    assert fail_record.id == "REQ-CONFIG-005"
    for record in (pass_record, fail_record):
        assert _GENERATED_ID.fullmatch(record.id) is None, (
            f"{record.id} is a generated ID; bindings must use the explicit ID declared "
            "by the Source of Truth"
        )

    # Declare the non-empty quality.gates entry and the requirement_verifiers binding only in
    # converge.yaml, then load it through the canonical configuration path.
    config_path = project / "converge.yaml"
    config_path.write_text(
        yaml.safe_dump(_config_data(pass_record.id, fail_record.id), sort_keys=True),
        encoding="utf-8",
    )
    cfg = load_config(config_path)

    assert cfg.requirements_path.resolve() == sot_path.resolve()
    assert cfg.auto_discover_quality is False
    assert len(cfg.quality_gates) == 1
    explicit = cfg.quality_gates[0]
    assert explicit.name == "explicit-configured-check"
    assert explicit.required is True
    assert explicit.command == [sys.executable, "-c", _GATE_SCRIPT]
    assert set(cfg.requirement_verifiers) == {pass_record.id, fail_record.id}
    for rules in cfg.requirement_verifiers.values():
        assert len(rules) == 1
        assert rules[0].required is True
        assert rules[0].command[0] == sys.executable

    # Configuration loading must not touch the Source of Truth.
    assert sot_path.read_bytes() == source_bytes_before
    assert sha256_file(sot_path) == source_digest_before

    # The explicit gate executes exactly the command configured in converge.yaml.
    gate_results = run_quality_gates(cfg, cfg.repo_path)
    assert [result.name for result in gate_results] == ["explicit-configured-check"]
    gate = gate_results[0]
    assert gate.required is True
    assert gate.returncode == 0
    assert gate.ok is True
    assert required_gates_pass(gate_results) is True
    assert _GATE_MARKER in gate.output
    # Observable side effect of the gate command: the configured script really ran.
    assert (repository / "gate-executed.txt").read_text(encoding="utf-8") == _GATE_MARKER

    # The bound requirements are enforced to PASS/FAIL by their configured verifiers.
    verifications = run_requirement_verifiers(cfg, cfg.repo_path, contract.requirements)
    by_id = {item.requirement_id: item for item in verifications}
    assert set(by_id) == {pass_record.id, fail_record.id}

    passing = by_id[pass_record.id]
    assert passing.status.value == "pass"
    assert passing.gates and passing.gates[0].ok is True
    assert passing.gates[0].returncode == 0
    assert any(pass_record.id in line for line in passing.evidence)

    failing = by_id[fail_record.id]
    assert failing.status.value == "fail"
    assert failing.gates and failing.gates[0].ok is False
    assert failing.gates[0].returncode == 1

    # REQ-CONFIG-004: after configuration loading and verifier execution the Source of Truth
    # is byte-identical in content and digest.
    assert sot_path.read_bytes() == source_bytes_before
    assert sha256_file(sot_path) == source_digest_before
