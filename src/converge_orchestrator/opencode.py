from pathlib import Path

from .models import AgentResult, ProjectConfig
from .shell import run


class OpenCodeAdapter:
    def __init__(self, config: ProjectConfig):
        self.config = config

    def invoke(self, role: str, prompt: str, cwd: Path) -> AgentResult:
        agent_cfg = self.config.agents[role]
        cmd = [self.config.opencode_binary, "run", "--agent", agent_cfg.agent]
        if agent_cfg.model:
            cmd += ["--model", agent_cfg.model]
        if self.config.opencode_attach_url:
            cmd += ["--attach", self.config.opencode_attach_url]
        cmd += ["--dir", str(cwd), prompt]
        result = run(cmd, cwd=cwd, timeout=agent_cfg.timeout_seconds)
        return AgentResult(
            role=role,
            ok=result.returncode == 0,
            output=result.stdout,
            returncode=result.returncode,
        )
