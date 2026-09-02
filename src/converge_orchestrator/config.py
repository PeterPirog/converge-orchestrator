from pathlib import Path

import yaml

from .models import ProjectConfig


def load_config(path: str | Path) -> ProjectConfig:
    source = Path(path).expanduser().resolve()
    data = yaml.safe_load(source.read_text(encoding="utf-8")) or {}
    cfg = ProjectConfig.model_validate(data)
    cfg.state_dir.mkdir(parents=True, exist_ok=True)
    cfg.worktree_dir.mkdir(parents=True, exist_ok=True)
    return cfg
