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
