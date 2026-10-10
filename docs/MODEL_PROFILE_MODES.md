# Cloud/local model profile modes

Converge supports two named routing modes, `cloud` and `local`, without changing LangGraph topology.
The active mode selects one profile set before the durable run configuration is pinned.

```yaml
models:
  mode: cloud
  profile_sets:
    cloud:
      scout: {...}
      planner: {...}
      builder: {...}
      builder_fallback: {...}
      reviewer: {...}
      security: {...}
```

The legacy `models.profiles` form remains supported. Do not combine it with `models.profile_sets`.

## Current reference routing

The current OpenWebUI catalog exposes stable IDs:
`deepseek-v4.1-flash:cloud`, `kimi-k2.7-code:cloud`, `glm-5.3-flash:cloud` and
`gemma4:31b-cloud`.

The reference template selects:

| Role | Model |
| --- | --- |
| Scout | `deepseek-v4.1-flash:cloud` |
| Planner | `deepseek-v4.1-flash:cloud` |
| Builder | `kimi-k2.7-code:cloud` |
| Builder fallback | `glm-5.3-flash:cloud` |
| Correctness Reviewer | `glm-5.3-flash:cloud` |
| Architecture Reviewer | `deepseek-v4.1-flash:cloud` |
| Security Reviewer | `deepseek-v4.1-flash:cloud` |

`gemma4:31b-cloud` is not assigned to a mandatory lane because the supplied catalog record has
`capabilities: null`. Arena IDs (`code-arena`, `code-arena-mid`, `math-arena`) are not used
because they do not preserve a stable underlying model identity for run provenance.

The supplied catalog does not publish reliable context/output limits, so the template leaves
`context_tokens` and `output_tokens` as `null` rather than copying limits from older model IDs.

## Local mode

`models.mode: local` remains supported, but the current template does not ship a stale local set.
Add `profile_sets.local` only after exact local IDs are visible through the current gateway and all
required roles have been benchmarked. Reduce `workflow.max_parallel_reviews` for limited hardware
instead of removing mandatory review lanes.

## Operational validation

```bash
converge models --config /path/to/converge.yaml
converge doctor --config /path/to/converge.yaml
```

Changing mode or model IDs never mutates an already-started run because the normalized run config is
hash-pinned.
