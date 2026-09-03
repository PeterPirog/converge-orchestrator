import json
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from pathlib import Path

from .context import (
    ContextBudgetExceeded,
    PromptEnvelope,
    append_context_ledger,
    finalize_report,
    prepare_prompt,
)
from .models import AgentResult, ProjectConfig, ReviewFinding, ReviewResult
from .opencode_config import (
    materialize_opencode_config,
    resolve_agent_model,
    resolve_agent_variant,
    resolve_profile_model,
    runtime_opencode_config,
)
from .sandbox import ExecutionSandbox

_PROVIDER_LEDGER_LOCK = threading.Lock()


def _json_object(text: str) -> dict:
    stripped = text.strip()
    try:
        payload = json.loads(stripped)
    except json.JSONDecodeError:
        start = stripped.find("{")
        end = stripped.rfind("}")
        if start < 0 or end <= start:
            raise ValueError("reviewer did not return a JSON object") from None
        payload = json.loads(stripped[start : end + 1])
    if not isinstance(payload, dict):
        raise ValueError("reviewer output must be a JSON object")
    return payload


def _failed_review(role: str, reason: str) -> ReviewResult:
    return ReviewResult(
        verdict="reject",
        findings=[
            ReviewFinding(
                severity="major",
                reason=reason,
                required_fix=f"{role} must complete successfully before integration",
                reviewer=role,
            )
        ],
        reviewers={role: "reject"},
    )


def _normalize_review(role: str, result: AgentResult) -> ReviewResult:
    if not result.ok:
        detail = result.output[-12000:]
        return _failed_review(
            role,
            f"{role} execution failed with exit code {result.returncode}: {detail}",
        )
    try:
        parsed = ReviewResult.model_validate(_json_object(result.output))
    except (ValueError, json.JSONDecodeError) as exc:
        return _failed_review(role, f"{role} returned invalid review JSON: {exc}")

    findings = [
        finding.model_copy(update={"reviewer": finding.reviewer or role})
        for finding in parsed.findings
    ]
    return parsed.model_copy(
        update={
            "findings": findings,
            "reviewers": {role: parsed.verdict},
        }
    )


def _aggregate_reviews(
    roles: list[str],
    results: dict[str, ReviewResult],
) -> ReviewResult:
    verdict = (
        "reject"
        if any(results[role].verdict == "reject" for role in roles)
        else "pass"
    )
    findings = [finding for role in roles for finding in results[role].findings]
    confidences = [
        results[role].confidence
        for role in roles
        if results[role].confidence is not None
    ]
    confidence = min(confidences) if confidences else None
    return ReviewResult(
        verdict=verdict,
        findings=findings,
        confidence=confidence,
        reviewers={role: results[role].verdict for role in roles},
    )


class OpenCodeAdapter:
    def __init__(self, config: ProjectConfig):
        self.config = config

    def invoke(self, role: str, prompt: str | PromptEnvelope, cwd: Path) -> AgentResult:
        generated_config = materialize_opencode_config(self.config)
        if role == "reviewer" and self.config.review_roles:
            return self._invoke_review_fanout(
                prompt,
                cwd,
                generated_config,
            )
        return self._invoke_with_resilience(
            role,
            prompt,
            cwd,
            generated_config,
        )

    def _append_provider_health(self, cwd: Path, payload: dict) -> None:
        target = self.config.state_dir / "provider-health.jsonl"
        target.parent.mkdir(parents=True, exist_ok=True)
        record = {
            "timestamp": datetime.now(UTC).isoformat(),
            "cwd": str(cwd.resolve()),
            **payload,
        }
        line = json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n"
        with _PROVIDER_LEDGER_LOCK:
            with target.open("a", encoding="utf-8") as fh:
                fh.write(line)

    def _invoke_with_resilience(
        self,
        role: str,
        prompt: str | PromptEnvelope,
        cwd: Path,
        generated_config: Path,
    ) -> AgentResult:
        agent_cfg = self.config.agents[role]
        profiles = [agent_cfg.model_profile, *agent_cfg.fallback_model_profiles]
        attempts: list[dict] = []
        final: AgentResult | None = None

        for profile_index, profile_name in enumerate(profiles):
            repeats = 1 + agent_cfg.provider_retries if profile_index == 0 else 1
            for retry_index in range(repeats):
                model = (
                    resolve_profile_model(self.config, self.config.model_profiles[profile_name])
                    if profile_name
                    else resolve_agent_model(self.config, agent_cfg)
                )
                try:
                    result = self._invoke_single_attempt(
                        role,
                        prompt,
                        cwd,
                        generated_config,
                        profile_name,
                    )
                    failure_kind = (
                        None
                        if result.ok
                        else "context_budget"
                        if result.returncode == 78
                        else "process_failure"
                    )
                except Exception as exc:
                    result = AgentResult(
                        role=role,
                        ok=False,
                        output=f"{role} execution raised {type(exc).__name__}: {exc}",
                        returncode=70,
                    )
                    failure_kind = "execution_exception"

                attempt = {
                    "attempt": len(attempts) + 1,
                    "model_profile": profile_name,
                    "model": model,
                    "primary": profile_index == 0,
                    "retry": retry_index,
                    "ok": result.ok,
                    "returncode": result.returncode,
                    "failure_kind": failure_kind,
                }
                attempts.append(attempt)
                self._append_provider_health(cwd, {"role": role, **attempt})
                final = result
                if result.ok:
                    return self._attach_attempts(result, attempts)
                if result.returncode == 78:
                    break

        assert final is not None
        return self._attach_attempts(final, attempts)

    @staticmethod
    def _attach_attempts(result: AgentResult, attempts: list[dict]) -> AgentResult:
        context = dict(result.context or {})
        selected = attempts[-1]
        context.update(
            {
                "provider_attempts": attempts,
                "selected_model": selected["model"],
                "selected_model_profile": selected["model_profile"],
                "fallback_used": not selected["primary"],
            }
        )
        return result.model_copy(update={"context": context})

    def _invoke_single_attempt(
        self,
        role: str,
        prompt: str | PromptEnvelope,
        cwd: Path,
        generated_config: Path,
        model_profile: str | None,
    ) -> AgentResult:
        try:
            rendered_prompt, context_report = prepare_prompt(
                self.config,
                role,
                prompt,
                model_profile=model_profile,
            )
        except ContextBudgetExceeded as exc:
            append_context_ledger(self.config, exc.report, cwd)
            return AgentResult(
                role=role,
                ok=False,
                output=f"CONTEXT_BUDGET_EXCEEDED: {exc}",
                returncode=78,
                context=exc.report.model_dump(mode="json"),
            )

        agent_cfg = self.config.agents[role]
        cmd = [self.config.opencode_binary, "run", "--agent", agent_cfg.agent]
        model = resolve_agent_model(self.config, agent_cfg, model_profile)
        variant = resolve_agent_variant(self.config, agent_cfg, model_profile)
        if model:
            cmd += ["--model", model]
        if variant:
            cmd += ["--variant", variant]
        if self.config.opencode_auto_approve:
            cmd.append("--auto")
        if self.config.opencode_attach_url:
            cmd += ["--attach", self.config.opencode_attach_url]
        # Fresh-session invariant: Converge never supplies --continue or --session. Long-running
        # continuity comes from LangGraph state and explicit evidence, not hidden model history.
        cmd += ["--dir", str(cwd), rendered_prompt]
        profile_overrides = {role: model_profile} if model_profile else None
        runtime_config = json.dumps(
            runtime_opencode_config(self.config, profile_overrides),
            separators=(",", ":"),
        )
        try:
            result = ExecutionSandbox(self.config).run(
                cmd,
                cwd=cwd,
                timeout=agent_cfg.timeout_seconds,
                env={
                    "OPENCODE_CONFIG": str(generated_config),
                    # Stable OpenCode loads inline config after project config and `.opencode`.
                    # This keeps orchestrator safety policy authoritative even for a target
                    # repository that contains its own OpenCode configuration.
                    "OPENCODE_CONFIG_CONTENT": runtime_config,
                },
                scope="agent",
                writable_cwd=role == "builder",
                include_state=True,
            )
        except Exception:
            append_context_ledger(self.config, context_report, cwd)
            raise
        context_report = finalize_report(context_report, result.stdout)
        append_context_ledger(self.config, context_report, cwd)
        return AgentResult(
            role=role,
            ok=result.returncode == 0,
            output=result.stdout,
            returncode=result.returncode,
            context=context_report.model_dump(mode="json"),
        )

    def _invoke_review_fanout(
        self,
        prompt: str | PromptEnvelope,
        cwd: Path,
        generated_config: Path,
    ) -> AgentResult:
        roles = list(self.config.review_roles)
        workers = min(self.config.max_parallel_reviews, len(roles))
        raw_results: dict[str, AgentResult] = {}
        failures: dict[str, Exception] = {}

        with ThreadPoolExecutor(
            max_workers=workers,
            thread_name_prefix="converge-review",
        ) as pool:
            futures = {
                role: pool.submit(
                    self._invoke_with_resilience,
                    role,
                    prompt,
                    cwd,
                    generated_config,
                )
                for role in roles
            }
            for role in roles:
                try:
                    raw_results[role] = futures[role].result()
                except Exception as exc:
                    # Timeout/runtime faults are evidence of a failed review, not a silent skip.
                    failures[role] = exc

        reviews: dict[str, ReviewResult] = {}
        for role in roles:
            if role in failures:
                failure = failures[role]
                reason = f"{role} execution raised {type(failure).__name__}: {failure}"
                reviews[role] = _failed_review(role, reason)
            else:
                reviews[role] = _normalize_review(role, raw_results[role])

        aggregate = _aggregate_reviews(roles, reviews)
        return AgentResult(
            role="reviewer",
            ok=True,
            output=aggregate.model_dump_json(),
            returncode=0,
            context={
                "review_lanes": {
                    role: raw_results[role].context
                    for role in roles
                    if role in raw_results
                }
            },
        )
