# Cloud/local model profile modes

Converge can keep two validated model-routing presets in one user-maintained `converge.yaml` and select
one of them with a single field:

```yaml
models:
  mode: local  # change to `cloud` when hosted quota/capacity is available again
  profile_sets:
    cloud:
      scout: {...}
      planner: {...}
      builder: {...}
      builder_fallback: {...}
      reviewer: {...}
      security: {...}
    local:
      scout: {...}
      planner: {...}
      builder: {...}
      builder_fallback: {...}
      reviewer: {...}
      security: {...}
```

The existing `models.profiles` form remains supported for backward compatibility. Do not combine
`models.profiles` with `models.profile_sets` in the same file.

Both `cloud` and `local` profile sets are schema-validated when the user YAML is loaded, including the
inactive set. This prevents the dormant rollback profile from silently rotting. Only the selected set is
normalized into `models.profiles` before `ProjectConfig` validation and only that selected set is written
to the immutable per-run configuration snapshot. Therefore changing `models.mode` after a run has
started cannot silently change the models of that durable run.

## Reference local routing

The reference template defaults to `models.mode: local` and uses self-hosted model IDs from the current
OpenWebUI/Ollama catalog:

| Role | Local model | Declared context | Rationale |
| --- | --- | ---: | --- |
| Scout | `nemotron-3.5-lightning:latest` | 1,048,576 | fast MoE agentic/tool-use model for repository mapping |
| Planner | `laguna-s-2.1:latest` | 262,144 | strong long-horizon software-engineering/repository reasoning |
| Builder | `qwen3.8:latest` | 262,144 | current local Qwen tool/reasoning model as the sole writer |
| Builder fallback | `nemotron-3.5-lightning:latest` | 1,048,576 | different family from primary Builder for bounded execution failover |
| Correctness Reviewer | `muse-glimmer:latest` | 131,072 | independent agentic/coding family from primary Builder |
| Architecture Reviewer | `laguna-s-2.1:latest` | 262,144 | same planning family in a fresh read-only reviewer session |
| Security Reviewer | `gpt-oss:120b` | 131,072 | large independent local reasoning family for security boundaries |

Context values deliberately follow the IDs/limits exposed by the deployment catalog rather than
assuming the maximum advertised by an upstream model card.

The cloud preset is retained verbatim in `profile_sets.cloud`:

- Scout: `deepseek-v4-flash:cloud`
- Planner / Architecture Reviewer: `deepseek-v4-pro:cloud`
- Builder: `kimi-k2.7-code:cloud`
- Builder fallback: `qwen3-coder-next:cloud`
- Correctness Reviewer: `glm-5.3-flash:cloud`
- Security Reviewer: `gpt-oss:120b`

Switching back therefore requires only:

```yaml
models:
  mode: cloud
```

No LangGraph topology, role permission, sandbox, quality gate, HITL or integration policy changes with
model mode. Deterministic gates remain authoritative in either mode.

## Operational validation

After changing mode, before starting a new run:

```bash
converge models --config /path/to/converge.yaml
converge doctor --config /path/to/converge.yaml
```

A durable run already has a hash-pinned normalized configuration snapshot. Do not change modes to try to
alter an already-started run; start a fresh run/state when the acceptance scenario itself requires a
fresh identity.

For memory-constrained self-hosted deployments, reduce `workflow.max_parallel_reviews` rather than
removing review lanes. That changes concurrency only; all correctness/architecture/security lanes still
remain mandatory.
