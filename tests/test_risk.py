from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

from converge_orchestrator.models import ProjectConfig, TaskEnvelope
from converge_orchestrator.risk import classify_repository_risk


def _config(tmp_path: Path) -> ProjectConfig:
    requirements = tmp_path / "architecture.md"
    requirements.write_text("System must remain safe.\n", encoding="utf-8")
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


def _classify(
    tmp_path: Path,
    path: str,
    *,
    base: str | None,
    candidate: str | None,
):
    cfg = _config(tmp_path)
    with (
        patch("converge_orchestrator.risk.changed_files", return_value=[path]),
        patch("converge_orchestrator.risk._read_base", return_value=base),
        patch("converge_orchestrator.risk._read_candidate", return_value=candidate),
    ):
        return classify_repository_risk(cfg, tmp_path, _task())


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


def test_secret_material_blocks_and_value_is_redacted(tmp_path: Path) -> None:
    secret = "ghp_ABCDEFGHIJKLMNOPQRSTUVWXYZ123456"
    report = _classify(
        tmp_path,
        "src/settings.py",
        base="TOKEN = None\n",
        candidate=f'TOKEN = "{secret}"\n',
    )

    assert "secret_material_detected" in report.flags
    finding = next(item for item in report.findings if item.kind == "secret_material")
    assert finding.disposition == "block"
    assert "redacted" in finding.evidence
    assert secret not in finding.evidence


def test_new_secret_dependency_interrupts_without_material_value(tmp_path: Path) -> None:
    report = _classify(
        tmp_path,
        "src/provider.py",
        base="",
        candidate='token = os.environ["PAYMENTS_API_KEY"]\n',
    )

    assert "secret_required" in report.flags
    finding = next(item for item in report.findings if item.kind == "secret_dependency")
    assert finding.disposition == "interrupt"
    assert "PAYMENTS_API_KEY" in finding.evidence


def test_placeholder_secret_literal_is_not_treated_as_material(tmp_path: Path) -> None:
    report = _classify(
        tmp_path,
        "examples/config.py",
        base="",
        candidate='password = "CHANGE_ME_PLACEHOLDER"\n',
    )

    assert "secret_material_detected" not in report.flags


def test_destructive_migration_interrupts_without_copying_sql(tmp_path: Path) -> None:
    sql = "ALTER TABLE customer DROP COLUMN legacy_id;"
    report = _classify(
        tmp_path,
        "migrations/0042_remove_customer.sql",
        base="-- old migration\n",
        candidate=f"{sql}\n",
    )

    assert "destructive_data_migration" in report.flags
    finding = next(item for item in report.findings if item.kind == "destructive_migration")
    assert finding.disposition == "interrupt"
    assert "destructive migration operation" in finding.evidence
    assert sql not in finding.evidence


def test_deleted_existing_migration_interrupts(tmp_path: Path) -> None:
    report = _classify(
        tmp_path,
        "alembic/versions/0042_users.py",
        base="def upgrade():\n    pass\n",
        candidate=None,
    )

    assert "destructive_data_migration" in report.flags


def test_python_public_function_signature_change_interrupts(tmp_path: Path) -> None:
    report = _classify(
        tmp_path,
        "src/payments/api.py",
        base="def charge(amount: int) -> str:\n    return str(amount)\n",
        candidate=(
            "def charge(amount: int, currency: str) -> str:\n"
            "    return f'{amount}:{currency}'\n"
        ),
    )

    assert "forbidden_public_api_change" in report.flags
    assert any("signature changed" in item.evidence for item in report.findings)


def test_python_private_symbol_change_does_not_interrupt(tmp_path: Path) -> None:
    report = _classify(
        tmp_path,
        "src/payments/api.py",
        base="def _normalize(value):\n    return value\n",
        candidate="def _normalize(value, strict=False):\n    return value\n",
    )

    assert "forbidden_public_api_change" not in report.flags


def test_python_explicit_exports_limit_public_surface(tmp_path: Path) -> None:
    report = _classify(
        tmp_path,
        "src/payments/api.py",
        base=(
            '__all__ = ["charge"]\n'
            "def charge(amount):\n    return amount\n"
            "def helper(value):\n    return value\n"
        ),
        candidate=(
            '__all__ = ["charge"]\n'
            "def charge(amount):\n    return amount\n"
            "def helper(value, strict=False):\n    return value\n"
        ),
    )

    assert "forbidden_public_api_change" not in report.flags


def test_node_removed_subpath_export_interrupts(tmp_path: Path) -> None:
    report = _classify(
        tmp_path,
        "package.json",
        base=(
            '{"name":"payments","exports":{'
            '".":"./dist/index.js","./testing":"./dist/testing.js"}}\n'
        ),
        candidate='{"name":"payments","exports":{".":"./dist/index.js"}}\n',
    )

    assert "forbidden_public_api_change" in report.flags
    assert any(
        item.evidence == "public Node package contract removed: package.json:exports:./testing"
        for item in report.findings
    )


def test_node_retargeted_conditional_export_is_observed_without_hitl(tmp_path: Path) -> None:
    report = _classify(
        tmp_path,
        "packages/client/package.json",
        base=(
            '{"name":"@example/client","exports":{'
            '"import":"./dist/index.mjs","require":"./dist/index.cjs"}}\n'
        ),
        candidate=(
            '{"name":"@example/client","exports":{'
            '"import":"./dist/v2.mjs","require":"./dist/index.cjs"}}\n'
        ),
    )

    assert "forbidden_public_api_change" not in report.flags
    finding = next(item for item in report.findings if "exports:.#import" in item.evidence)
    assert finding.disposition == "observe"


def test_node_removed_conditional_export_interrupts(tmp_path: Path) -> None:
    report = _classify(
        tmp_path,
        "package.json",
        base=(
            '{"name":"payments","exports":{'
            '"import":"./dist/index.mjs","require":"./dist/index.cjs"}}\n'
        ),
        candidate=(
            '{"name":"payments","exports":{'
            '"import":"./dist/index.mjs"}}\n'
        ),
    )

    assert "forbidden_public_api_change" in report.flags
    assert any("exports:.#require" in item.evidence for item in report.findings)


def test_node_removed_cli_interrupts_and_retargeted_cli_is_observed(tmp_path: Path) -> None:
    report = _classify(
        tmp_path,
        "package.json",
        base='{"name":"converge","bin":{"converge":"./bin/cli.js","cv":"./bin/cli.js"}}\n',
        candidate='{"name":"converge","bin":{"converge":"./bin/v2.js"}}\n',
    )

    assert "forbidden_public_api_change" in report.flags
    evidence = {item.evidence for item in report.findings}
    assert "public Node package target changed: package.json:bin:converge" in evidence
    assert "public Node package contract removed: package.json:bin:cv" in evidence


def test_node_additive_exports_do_not_interrupt(tmp_path: Path) -> None:
    report = _classify(
        tmp_path,
        "package.json",
        base='{"name":"payments","exports":{".":"./dist/index.js"}}\n',
        candidate=(
            '{"name":"payments","exports":{'
            '".":"./dist/index.js","./testing":"./dist/testing.js"}}\n'
        ),
    )

    assert "forbidden_public_api_change" not in report.flags


def test_node_legacy_entrypoint_change_is_observed_without_hitl(tmp_path: Path) -> None:
    report = _classify(
        tmp_path,
        "package.json",
        base=(
            '{"name":"payments","main":"./dist/index.js",'
            '"types":"./dist/index.d.ts"}\n'
        ),
        candidate=(
            '{"name":"payments","main":"./dist/v2.js",'
            '"types":"./dist/index.d.ts"}\n'
        ),
    )

    assert "forbidden_public_api_change" not in report.flags
    finding = next(item for item in report.findings if item.evidence.endswith("package.json:main"))
    assert finding.disposition == "observe"


def test_node_package_name_change_interrupts(tmp_path: Path) -> None:
    report = _classify(
        tmp_path,
        "package.json",
        base='{"name":"payments","main":"./index.js"}\n',
        candidate='{"name":"payments-v2","main":"./index.js"}\n',
    )

    assert "forbidden_public_api_change" in report.flags
    assert any("package name changed" in item.evidence for item in report.findings)


def test_unrelated_node_manifest_changes_do_not_interrupt(tmp_path: Path) -> None:
    report = _classify(
        tmp_path,
        "package.json",
        base='{"name":"payments","main":"./index.js","scripts":{"test":"vitest"}}\n',
        candidate=(
            '{"name":"payments","main":"./index.js",'
            '"scripts":{"test":"vitest run","lint":"eslint ."}}\n'
        ),
    )

    assert "forbidden_public_api_change" not in report.flags


def test_node_deleted_published_target_interrupts_even_when_manifest_is_unchanged(
    tmp_path: Path,
) -> None:
    manifest = (
        '{"name":"payments","exports":{'
        '".":{"import":"./dist/index.mjs","require":"./dist/index.cjs"}},'
        '"types":"./dist/index.d.ts"}\n'
    )
    report = _classify_sources(
        tmp_path,
        ["dist/index.mjs"],
        base={
            "package.json": manifest,
            "dist/index.mjs": "export const charge = () => true;\n",
        },
        candidate={
            "package.json": manifest,
            "dist/index.mjs": None,
        },
    )

    assert "forbidden_public_api_change" in report.flags
    finding = next(
        item
        for item in report.findings
        if "still published" in item.evidence
    )
    assert finding.disposition == "interrupt"
    assert finding.path == "dist/index.mjs"
    assert "package.json:exports:.#import -> ./dist/index.mjs" in finding.evidence


def test_node_retargeted_public_entry_can_remove_old_target_without_hitl(
    tmp_path: Path,
) -> None:
    report = _classify_sources(
        tmp_path,
        ["package.json", "dist/index.js", "dist/v2.js"],
        base={
            "package.json": '{"name":"payments","main":"./dist/index.js"}\n',
            "dist/index.js": "module.exports = require('./runtime');\n",
            "dist/v2.js": None,
        },
        candidate={
            "package.json": '{"name":"payments","main":"./dist/v2.js"}\n',
            "dist/index.js": None,
            "dist/v2.js": "module.exports = require('./runtime');\n",
        },
    )

    assert "forbidden_public_api_change" not in report.flags
    finding = next(item for item in report.findings if item.evidence.endswith("package.json:main"))
    assert finding.disposition == "observe"


def test_node_nested_package_target_deletion_is_bound_to_nearest_manifest(
    tmp_path: Path,
) -> None:
    manifest = '{"name":"@example/client","types":"./dist/index.d.ts"}\n'
    report = _classify_sources(
        tmp_path,
        ["packages/client/dist/index.d.ts"],
        base={
            "packages/client/package.json": manifest,
            "packages/client/dist/index.d.ts": "export declare function charge(): void;\n",
        },
        candidate={
            "packages/client/package.json": manifest,
            "packages/client/dist/index.d.ts": None,
        },
    )

    assert "forbidden_public_api_change" in report.flags
    assert any(
        item.evidence
        == (
            "public Node package target removed while still published: "
            "packages/client/package.json:types -> ./dist/index.d.ts"
        )
        for item in report.findings
    )


def test_auth_weakening_interrupts_but_test_changes_do_not(tmp_path: Path) -> None:
    report = _classify(
        tmp_path,
        "src/security/auth.py",
        base="def verify(token):\n    return validate(token)\n",
        candidate="def verify(token):\n    return decode(token, verify=False)\n",
    )
    assert "critical_auth_redesign" in report.flags

    test_report = _classify(
        tmp_path,
        "tests/security/test_auth.py",
        base="def test_auth():\n    assert True\n",
        candidate="def test_auth():\n    assert verify_token()\n",
    )
    assert "critical_auth_redesign" not in test_report.flags


def test_small_auth_surface_change_is_observed_not_interrupted(tmp_path: Path) -> None:
    report = _classify(
        tmp_path,
        "src/auth/session.py",
        base="def load_session(token):\n    return old_loader(token)\n",
        candidate="def load_session(token):\n    return new_loader(token)\n",
    )

    assert "critical_auth_redesign" not in report.flags
    finding = next(item for item in report.findings if item.kind == "auth_security_change")
    assert finding.disposition == "observe"


def test_lost_security_primitive_interrupts(tmp_path: Path) -> None:
    report = _classify(
        tmp_path,
        "src/auth/authorization.py",
        base=(
            "def authorize(user):\n"
            "    if not has_permission(user):\n"
            "        raise PermissionError\n"
            "    return True\n"
        ),
        candidate="def authorize(user):\n    return True\n",
    )

    assert "critical_auth_redesign" in report.flags
    finding = next(item for item in report.findings if item.kind == "auth_security_change")
    assert finding.disposition == "interrupt"
    assert "permission" in finding.evidence


def test_author_file_is_not_false_positive_auth_path(tmp_path: Path) -> None:
    report = _classify(
        tmp_path,
        "src/author.py",
        base="def author_name():\n    return 'old'\n",
        candidate="def author_name():\n    return 'new'\n",
    )

    assert not any(item.kind == "auth_security_change" for item in report.findings)
