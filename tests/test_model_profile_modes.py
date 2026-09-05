from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from converge_orchestrator.config import (
    load_config,
    materialize_run_config_snapshot,
)
from converge_orchestrator.opencode_config import build_opencode_config


def _write_config(tmp_path: Path, *, mode: str = "local") -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    requirements = tmp_path / "requirements.md"
    requirements.write_text("System must remain testable.\n", encoding="utf-8")
    config = {
        "version": 1,
        "project": {
            "name": "profile-mode-test",
            "repo_path": str(repo),
            "requirements_path": str(requirements),
            "state_dir": str(tmp_path / "state"),
            "require_spec_read_only": False,
        },
        "models": {
            "gateway": {
                "kind": "openwebui",
                "base_url": "http://127.0.0.1:3000/api",
                "api_key_env": "OPENWEBUI_API_KEY",
            },
            "mode": mode,
            "profile_sets": {
                "cloud": {
                    "planner": {
                        "model": "planner-cloud",
                        "context_tokens": 100000,
                    },
                    "builder": {
                        "model": "builder-cloud",
                        "context_tokens": 64000,
                    },
                },
                "local": {
                    "planner": {
                        "model": "planner-local",
                        "context_tokens": 200000,
                    },
                    "builder": {
                        "model": "builder-local",
                        "context_tokens": 128000,
                    },
                },
            },
        },
        "agents": {
            "planner": {
                "agent": "converge-planner",
                "model_profile": "planner",
            },
            "builder": {
                "agent": "converge-builder",
                "model_profile": "builder",
            },
        },
    }
    path = tmp_path / "converge.yaml"
    path.write_text(yaml.safe_dump(config, sort_keys=False), encoding="utf-8")
    return path


def test_local_mode_selects_only_local_profiles(tmp_path: Path) -> None:
    cfg = load_config(_write_config(tmp_path, mode="local"))

    assert cfg.model_profiles["planner"].model == "planner-local"
    assert cfg.model_profiles["builder"].model == "builder-local"

    provider_models = build_opencode_config(cfg)["provider"]["openwebui"]["models"]
    assert set(provider_models) == {"planner-local", "builder-local"}


def test_cloud_mode_selects_only_cloud_profiles(tmp_path: Path) -> None:
    cfg = load_config(_write_config(tmp_path, mode="cloud"))

    assert cfg.model_profiles["planner"].model == "planner-cloud"
    assert cfg.model_profiles["builder"].model == "builder-cloud"

    provider_models = build_opencode_config(cfg)["provider"]["openwebui"]["models"]
    assert set(provider_models) == {"planner-cloud", "builder-cloud"}


def test_profile_mode_rejects_unknown_mode(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="models.mode must be one of"):
        load_config(_write_config(tmp_path, mode="hybrid"))


def test_profile_sets_cannot_be_combined_with_legacy_profiles(tmp_path: Path) -> None:
    path = _write_config(tmp_path)
    raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    raw["models"]["profiles"] = {
        "planner": {"model": "ambiguous-model"},
    }
    path.write_text(yaml.safe_dump(raw, sort_keys=False), encoding="utf-8")

    with pytest.raises(ValueError, match="cannot be combined"):
        load_config(path)


def test_inactive_profile_set_is_validated_before_switch(tmp_path: Path) -> None:
    path = _write_config(tmp_path, mode="local")
    raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    raw["models"]["profile_sets"]["cloud"]["planner"]["context_tokens"] = 0
    path.write_text(yaml.safe_dump(raw, sort_keys=False), encoding="utf-8")

    with pytest.raises(ValueError, match="models.profile_sets.cloud.planner"):
        load_config(path)


def test_run_snapshot_pins_only_the_selected_profile_set(tmp_path: Path) -> None:
    path = _write_config(tmp_path, mode="local")

    cfg, snapshot, digest = materialize_run_config_snapshot(path, "run-local")
    raw = yaml.safe_load(snapshot.read_text(encoding="utf-8"))

    assert cfg.model_profiles["planner"].model == "planner-local"
    assert len(digest) == 64
    assert "mode" not in raw["models"]
    assert "profile_sets" not in raw["models"]
    assert raw["models"]["profiles"]["planner"]["model"] == "planner-local"
    assert raw["models"]["profiles"]["builder"]["model"] == "builder-local"
