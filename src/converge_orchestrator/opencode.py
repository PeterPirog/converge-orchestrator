import json
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from .context import (
    ContextBudgetExceeded,
    PromptEnvelope,
    append_context_ledger,
    finalize_report,
    prepare_prompt,
)
from .models import AgentConfig, AgentResult, ProjectConfig, ReviewFinding, ReviewResult
from .opencode_config import (
    materialize_opencode_config,
    resolve_agent_model,
    resolve_agent_variant,
    runtime_opencode_config,
)
from .sandbox import ExecutionSandbox


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
        return self._invoke_single(
            role,
            prompt,
            cwd,
            generated_config,
        )

    def _attempt_profiles(self, agent_cfg: AgentConfig) -> list[str | None]:
        if not agent_cfg.model_profile:
            return [None]
        return [agent_cfg.model_profile, *agent_cfg.fallback_model_profiles]

    def _config_for_attempt(self, role: str, profile_name: str | None) -> ProjectConfig:
        agent_cfg = self.config.agents[role]
        if profile_name is None or profile_name == agent_cfg.model_profile:
            return self.config
        attempt_config = self.config.model_copy(deep=True)
        attempt_config.agents[role] = attempt_config.agents[role].model_copy(
            update={
                "model": None,
                "model_profile": profile_name,
                "fallback_model_profiles": [],
            }
        )
        return attempt_config

    @staticmethod
    def _attempt_evidence(
        *,
        index: int,
        profile_name: str | None,
        model: str | None,
        variant: str | None,
        fallback: bool,
        returncode: int | None = None,
        error_type: str | None = None,
    ) -> dict:
        if error_type is not None:
            outcome = "exception"
        elif returncode == 0:
            outcome = "success"
        else:
            outcome = "nonzero"
        return {
            "attempt": index,
            "profile": profile_name,
            "model": model,
            "variant": variant,
            "fallback": fallback,
            "outcome": outcome,
            "returncode": returncode,
            "error_type": error_type,
        }

    def _invoke_single(
        self,
        role: str,
        prompt: str | PromptEnvelope,
        cwd: Path,
        generated_config: Path,
    ) -> AgentResult:
        try:
            rendered_prompt, context_report = prepare_prompt(self.config, role, prompt)
        except ContextBudgetExceeded as exc:
            append_context_ledger(self.config, exc.report, cwd)
            return AgentResult(
                role=role,
                ok=False,
                output=f"CONTEXT_BUDGET_EXCEEDED: {exc}",
                returncode=78,
                context=exc.report.model_dump(mode="json"),
            )

        primary_agent_cfg = self.config.agents[role]
        profiles = self._attempt_profiles(primary_agent_cfg)
        attempts: list[dict] = []
        last_result = None

        for attempt_index, profile_name in enumerate(profiles, start=1):
            attempt_config = self._config_for_attempt(role, profile_name)
            agent_cfg = attempt_config.agents[role]
            runtime_config = json.dumps(
                runtime_opencode_config(attempt_config),
                separators=(",", ":"),
            )
            cmd = [self.config.opencode_binary, "run", "--agent", agent_cfg.agent]
            model = resolve_agent_model(attempt_config, agent_cfg)
            variant = resolve_agent_variant(attempt_config, agent_cfg)
            if model:
                cmd += ["--model", model]
            if variant:
                cmd += ["--variant", variant]
            if self.config.opencode_auto_approve:
                cmd.append("--auto")
            if self.config.opencode_attach_url:
                cmd += ["--attach", self.config.opencode_attach_url]
            # Fresh-session invariant: every fallback attempt is a new OpenCode session with the
            # exact same already-budgeted prompt. Continuity stays in LangGraph state/evidence.
            cmd += ["--dir", str(cwd), rendered_prompt]

            try:
                result = ExecutionSandbox(self.config).run(
                    cmd,
                    cwd=cwd,
                    timeout=primary_agent_cfg.timeout_seconds,
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
            except Exception as exc:
                attempts.append(
                    self._attempt_evidence(
                        index=attempt_index,
                        profile_name=profile_name,
                        model=model,
                        variant=variant,
                        fallback=attempt_index > 1,
                        error_type=type(exc).__name__,
                    )
                )
                if attempt_index < len(profiles):
                    continue
                report = context_report.model_copy(update={"model_attempts": attempts})
                append_context_ledger(self.config, report, cwd)
                raise

            last_result = result
            attempts.append(
                self._attempt_evidence(
                    index=attempt_index,
                    profile_name=profile_name,
                    model=model,
                    variant=variant,
                    fallback=attempt_index > 1,
                    returncode=result.returncode,
                )
            )
            if result.returncode == 0:
                break

        if last_result is None:
            raise RuntimeError(f"OpenCode role {role!r} produced no execution result")

        context_report = context_report.model_copy(update={"model_attempts": attempts})
        context_report = finalize_report(context_report, last_result.stdout)
        append_context_ledger(self.config, context_report, cwd)
        return AgentResult(
            role=role,
            ok=last_result.returncode == 0,
            output=last_result.stdout,
            returncode=last_result.returncode,
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
                    self._invoke_single,
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
