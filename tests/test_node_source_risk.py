from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

from converge_orchestrator.models import ProjectConfig, TaskEnvelope
from converge_orchestrator.risk import classify_repository_risk


def _config(tmp_path: Path) -> ProjectConfig:
    requirements = tmp_path / "architecture.md"
    requirements.write_text("System must preserve public compatibility.\n", encoding="utf-8")
    return ProjectConfig(
        repo_path=tmp_path,
        requirements_path=requirements,
        require_spec_read_only=False,
        agents={},
    )


def _task() -> TaskEnvelope:
    return TaskEnvelope(
        id="ARCH-001-1",
        requirement_ids=["ARCH-001"],
        title="Change",
        objective="Change safely",
        allowed_paths=["**"],
    )


def _classify_sources(
    tmp_path: Path,
    paths: list[str],
    *,
    base: dict[str, str | None],
    candidate: dict[str, str | None],
):
    cfg = _config(tmp_path)
    with (
        patch("converge_orchestrator.risk.changed_files", return_value=paths),
        patch(
            "converge_orchestrator.risk._read_base",
            side_effect=lambda _cwd, _branch, path: base.get(path),
        ),
        patch(
            "converge_orchestrator.risk._read_candidate",
            side_effect=lambda _cwd, path: candidate.get(path),
        ),
    ):
        return classify_repository_risk(cfg, tmp_path, _task())


def test_removed_named_export_from_unchanged_public_target_interrupts(tmp_path: Path) -> None:
    manifest = '{"name":"payments","exports":"./src/index.ts"}\n'
    report = _classify_sources(
        tmp_path,
        ["src/index.ts"],
        base={
            "package.json": manifest,
            "src/index.ts": (
                "export function charge(amount: number) { return amount; }\n"
                "export const version = '1';\n"
            ),
        },
        candidate={
            "package.json": manifest,
            "src/index.ts": "export const version = '1';\n",
        },
    )

    assert "forbidden_public_api_change" in report.flags
    finding = next(item for item in report.findings if "source export removed" in item.evidence)
    assert finding.disposition == "interrupt"
    assert finding.path == "src/index.ts"
    assert finding.evidence.endswith("package.json:exports:. -> ./src/index.ts:charge")


def test_additive_named_export_does_not_interrupt(tmp_path: Path) -> None:
    manifest = '{"name":"payments","exports":"./src/index.ts"}\n'
    report = _classify_sources(
        tmp_path,
        ["src/index.ts"],
        base={
            "package.json": manifest,
            "src/index.ts": "export const version = '1';\n",
        },
        candidate={
            "package.json": manifest,
            "src/index.ts": (
                "export const version = '1';\n"
                "export function charge(amount: number) { return amount; }\n"
            ),
        },
    )

    assert "forbidden_public_api_change" not in report.flags
    assert not any("source export removed" in item.evidence for item in report.findings)


def test_incomplete_candidate_surface_is_observed_not_interrupted(tmp_path: Path) -> None:
    manifest = '{"name":"payments","exports":"./src/index.ts"}\n'
    report = _classify_sources(
        tmp_path,
        ["src/index.ts"],
        base={
            "package.json": manifest,
            "src/index.ts": "export const charge = 1;\n",
        },
        candidate={
            "package.json": manifest,
            "src/index.ts": 'export * from "./api";\n',
        },
    )

    assert "forbidden_public_api_change" not in report.flags
    finding = next(item for item in report.findings if "could not be proven" in item.evidence)
    assert finding.disposition == "observe"


def test_manifest_retarget_does_not_turn_old_source_removal_into_hitl(tmp_path: Path) -> None:
    report = _classify_sources(
        tmp_path,
        ["package.json", "src/index.ts", "src/v2.ts"],
        base={
            "package.json": '{"name":"payments","exports":"./src/index.ts"}\n',
            "src/index.ts": "export const charge = 1;\n",
            "src/v2.ts": None,
        },
        candidate={
            "package.json": '{"name":"payments","exports":"./src/v2.ts"}\n',
            "src/index.ts": None,
            "src/v2.ts": "export const charge = 1;\n",
        },
    )

    assert "forbidden_public_api_change" not in report.flags
    manifest_finding = next(
        item for item in report.findings if item.evidence.endswith("package.json:exports:.")
    )
    assert manifest_finding.disposition == "observe"


def test_typescript_declaration_target_lost_export_interrupts(tmp_path: Path) -> None:
    manifest = '{"name":"payments","types":"./dist/index.d.ts"}\n'
    report = _classify_sources(
        tmp_path,
        ["dist/index.d.ts"],
        base={
            "package.json": manifest,
            "dist/index.d.ts": (
                "export interface Receipt { id: string }\n"
                "export declare function charge(amount: number): Receipt;\n"
            ),
        },
        candidate={
            "package.json": manifest,
            "dist/index.d.ts": "export interface Receipt { id: string }\n",
        },
    )

    assert "forbidden_public_api_change" in report.flags
    assert any(item.evidence.endswith("./dist/index.d.ts:charge") for item in report.findings)


def test_incomplete_baseline_surface_is_not_used_as_false_proof(tmp_path: Path) -> None:
    manifest = '{"name":"payments","exports":"./src/index.ts"}\n'
    report = _classify_sources(
        tmp_path,
        ["src/index.ts"],
        base={
            "package.json": manifest,
            "src/index.ts": 'export * from "./api";\n',
        },
        candidate={
            "package.json": manifest,
            "src/index.ts": "export const version = '2';\n",
        },
    )

    assert "forbidden_public_api_change" not in report.flags
    assert not any("source export removed" in item.evidence for item in report.findings)
