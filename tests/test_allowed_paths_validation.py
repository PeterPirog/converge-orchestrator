from __future__ import annotations

from pathlib import Path

from converge_orchestrator.workflow import _invalid_allowed_paths_error


def _repo(tmp_path: Path) -> Path:
    """Minimum faithful ACCEPT-003 layout: no src/, code under shared_tools/."""
    (tmp_path / "shared_tools").mkdir()
    (tmp_path / "shared_tools" / "__init__.py").write_text("", encoding="utf-8")
    (tmp_path / "shared_tools" / "fake_terminal.py").write_text("value = 1\n", encoding="utf-8")
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "test_shared_tools_fake_terminal.py").write_text(
        "import shared_tools.fake_terminal\n",
        encoding="utf-8",
    )
    (tmp_path / "README.md").write_text("# repo\n", encoding="utf-8")
    return tmp_path


def test_schema_example_pattern_in_repo_without_src_is_invalid(tmp_path: Path) -> None:
    error = _invalid_allowed_paths_error(_repo(tmp_path), ["src/**", "tests/**"])

    assert error is not None
    assert "'src/**'" in error
    assert "shared_tools/" in error
    assert "tests/" in error


def test_real_repo_layouts_validate(tmp_path: Path) -> None:
    repo = _repo(tmp_path)

    assert _invalid_allowed_paths_error(repo, ["shared_tools/**", "tests/**"]) is None
    assert _invalid_allowed_paths_error(repo, ["shared_tools/fake_terminal.py"]) is None
    assert _invalid_allowed_paths_error(repo, ["tests/test_shared_tools_fake_terminal.py"]) is None


def test_new_file_in_existing_directory_is_allowed(tmp_path: Path) -> None:
    repo = _repo(tmp_path)

    assert _invalid_allowed_paths_error(repo, ["tests/test_new_file.py"]) is None
    assert _invalid_allowed_paths_error(repo, ["shared_tools/extra.py"]) is None


def test_new_file_in_missing_directory_is_invalid(tmp_path: Path) -> None:
    error = _invalid_allowed_paths_error(_repo(tmp_path), ["src/new_file.py"])

    assert error is not None
    assert "'src/new_file.py'" in error


def test_leading_glob_matches_anywhere(tmp_path: Path) -> None:
    repo = _repo(tmp_path)

    assert _invalid_allowed_paths_error(repo, ["*.md", "**/*.py"]) is None


def test_nested_glob_base_directory_must_exist(tmp_path: Path) -> None:
    error = _invalid_allowed_paths_error(_repo(tmp_path), ["shared_tools/nested/x*.py"])

    assert error is not None
    assert "'shared_tools/nested/x*.py'" in error


def test_empty_patterns_and_missing_repo_are_not_contract_errors(tmp_path: Path) -> None:
    repo = _repo(tmp_path)

    assert _invalid_allowed_paths_error(repo, []) is None
    assert _invalid_allowed_paths_error(tmp_path / "missing", ["src/**"]) is None