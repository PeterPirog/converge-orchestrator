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


def test_removed_export_behind_unchanged_public_barrel_interrupts(tmp_path: Path) -> None:
    manifest = '{"name":"payments","exports":"./src/index.ts"}\n'
    barrel = 'export * from "./api";\n'
    report = _classify_sources(
        tmp_path,
        ["src/api.ts"],
        base={
            "package.json": manifest,
            "src/index.ts": barrel,
            "src/api.ts": (
                "export function charge(amount: number) { return amount; }\n"
                "export const version = '1';\n"
            ),
        },
        candidate={
            "package.json": manifest,
            "src/index.ts": barrel,
            "src/api.ts": "export const version = '1';\n",
        },
    )

    assert "forbidden_public_api_change" in report.flags
    finding = next(item for item in report.findings if "source export removed" in item.evidence)
    assert finding.disposition == "interrupt"
    assert finding.path == "src/api.ts"
    assert finding.evidence.endswith("package.json:exports:. -> ./src/index.ts:charge")


def test_required_arity_increase_behind_unchanged_barrel_interrupts(tmp_path: Path) -> None:
    manifest = '{"name":"payments","exports":"./src/index.ts"}\n'
    barrel = 'export * from "./api";\n'
    report = _classify_sources(
        tmp_path,
        ["src/api.ts"],
        base={
            "package.json": manifest,
            "src/index.ts": barrel,
            "src/api.ts": "export function charge(amount: number): void {}\n",
        },
        candidate={
            "package.json": manifest,
            "src/index.ts": barrel,
            "src/api.ts": (
                "export function charge(amount: number, currency: string): void {}\n"
            ),
        },
    )

    assert "forbidden_public_api_change" in report.flags
    finding = next(
        item for item in report.findings if "minimum argument count increased" in item.evidence
    )
    assert finding.path == "src/api.ts"
    assert finding.evidence.endswith("./src/index.ts:charge (1 -> 2)")


def test_new_ambiguous_local_target_is_observed_not_interrupted(tmp_path: Path) -> None:
    manifest = '{"name":"payments","exports":"./src/index.ts"}\n'
    barrel = 'export * from "./api";\n'
    report = _classify_sources(
        tmp_path,
        ["src/api.js"],
        base={
            "package.json": manifest,
            "src/index.ts": barrel,
            "src/api.ts": "export const stable = true;\n",
            "src/api.js": None,
        },
        candidate={
            "package.json": manifest,
            "src/index.ts": barrel,
            "src/api.ts": "export const stable = true;\n",
            "src/api.js": "export const runtime = true;\n",
        },
    )

    assert "forbidden_public_api_change" not in report.flags
    finding = next(item for item in report.findings if "could not be proven" in item.evidence)
    assert finding.disposition == "observe"
    assert finding.path == "src/api.js"


def test_unresolved_external_barrel_is_not_promoted_to_false_hitl(tmp_path: Path) -> None:
    manifest = '{"name":"payments","exports":"./src/index.ts"}\n'
    barrel = 'export * from "external-package";\n'
    report = _classify_sources(
        tmp_path,
        ["src/internal.ts"],
        base={
            "package.json": manifest,
            "src/index.ts": barrel,
            "src/internal.ts": "export const value = 1;\n",
        },
        candidate={
            "package.json": manifest,
            "src/index.ts": barrel,
            "src/internal.ts": "export const value = 2;\n",
        },
    )

    assert "forbidden_public_api_change" not in report.flags
    assert not any("could not be proven" in item.evidence for item in report.findings)
