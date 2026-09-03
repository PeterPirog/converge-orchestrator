import json
from pathlib import Path

from .models import AgentResult, ProjectConfig
from .opencode_config import (
    materialize_opencode_config,
    resolve_agent_model,
    resolve_agent_variant,
    runtime_opencode_config,
)
from .shell import run


class OpenCodeAdapter:
    def __init__(self, config: ProjectConfig):
        self.config = config

    def invoke(self, role: str, prompt: str, cwd: Path) -> AgentResult:
        agent_cfg = self.config.agents[role]
        generated_config = materialize_opencode_config(self.config)
        runtime_config = json.dumps(runtime_opencode_config(self.config), separators=(",", ":"))
        cmd = [self.config.opencode_binary, "run", "--agent", agent_cfg.agent]
        model = resolve_agent_model(self.config, agent_cfg)
        variant = resolve_agent_variant(self.config, agent_cfg)
        if model:
            cmd += ["--model", model]
        if variant:
            cmd += ["--variant", variant]
        if self.config.opencode_auto_approve:
            cmd.append("--auto")
        if self.config.opencode_attach_url:
            cmd += ["--attach", self.config.opencode_attach_url]
        cmd += ["--dir", str(cwd), prompt]
        result = run(
            cmd,
            cwd=cwd,
            timeout=agent_cfg.timeout_seconds,
            env={
                "OPENCODE_CONFIG": str(generated_config),
                # Stable OpenCode loads inline config after project config and `.opencode`. This
                # keeps orchestrator safety policy authoritative even for a target repository that
                # contains its own OpenCode configuration.
                "OPENCODE_CONFIG_CONTENT": runtime_config,
            },
        )
        return AgentResult(
            role=role,
            ok=result.returncode == 0,
            output=result.stdout,
            returncode=result.returncode,
        )
