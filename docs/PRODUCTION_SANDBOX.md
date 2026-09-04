# Production sandbox profile

This profile is the minimum supported OS/container boundary for unattended Converge execution. It is
execution policy, not a second requirements source: the immutable Markdown architecture remains the
only Source of Truth and LangGraph remains the durable workflow authority.

## Required properties

Production autonomous runs use `sandbox.mode: container` and an immutable image reference. Converge
rejects mutable image tags before Docker is invoked. Accepted references are:

```text
ghcr.io/acme/payments-converge-runtime@sha256:<64 lowercase hex digits>
sha256:<64 lowercase hex digits>  # local content-addressed image ID, mainly useful for CI
```

The image must already exist locally. Converge always uses `--pull=never`; runtime execution therefore
cannot silently replace the pinned toolchain. Registry provisioning/pulling is an operator/deployment
step performed before `converge doctor`.

The image must contain stable OpenCode plus the exact build/test/typecheck/verification toolchain needed
by the target repository. Do not build secrets into the image. Gateway and MCP credentials stay in
allowlisted environment variables and are scoped to the agent role that needs them.

A production project should use a profile equivalent to:

```yaml
sandbox:
  mode: container
  engine: docker
  image: ghcr.io/acme/payments-converge-runtime@sha256:<digest>
  agent_network: converge-ai
  quality_network: none
  agent_gateway_base_url: http://open-webui:8080/api
  require_internal_agent_network: true
  read_only_root: true
  pids_limit: 512
  memory: 8g
  cpus: 4.0
  tmpfs_size: 2g
  pass_env: []
  user: host

workflow:
  run_budget:
    max_wall_time_seconds: 86400
    max_model_attempts: 512
    max_estimated_tokens: 8000000
```

Resource values are project policy and should be reduced after representative measurements. The point
of the profile is that all values are finite and pinned with the durable run; an LLM cannot enlarge or
waive them.

## Network boundary

When `require_internal_agent_network: true`, `agent_network` must be a named Docker network with
`Internal=true`. For example:

```bash
docker network create --internal converge-ai
```

OpenWebUI/model gateway and any deliberately allowed remote MCP proxy must be reachable on that
network. Quality gates default to `quality_network: none`; broaden network access only for deterministic
tests that genuinely need it.

`opencode.attach_url` is forbidden in container mode because an attached OpenCode server would execute
tools outside the container boundary. A loopback model gateway is also rejected because container
loopback is not the host gateway.

## Preflight

Before an unattended run:

```bash
converge doctor --config /path/to/converge.yaml
```

The preflight verifies the configured engine, immutable image reference, local image availability,
network existence/internal status and container-specific runtime constraints. It never pulls an image
or creates a network implicitly.

Recommended deployment sequence:

1. build and test the project runtime image in a separate image pipeline;
2. publish it and record the registry digest, or resolve a local test image to its content-addressed
   `sha256:` ID;
3. put that immutable reference in `converge.yaml`;
4. provision the internal agent network and gateway/MCP services;
5. run `converge doctor`;
6. only then start the durable autonomous run.

Changing the source YAML later cannot change an already active run because Converge uses the normalized
SHA-256-pinned run configuration snapshot.

## CI proof in Converge itself

The Converge CI matrix includes a real `sandbox-container` job. It prepares an image fixture, converts
it to the local content-addressed image ID, creates an internal Docker network, executes the real agent
sandbox path and verifies that a read-only repository mount cannot be modified. Unit tests separately
prove that mutable image tags fail before Docker execution.

This CI fixture proves Converge's sandbox contract, not the contents of a user's project-specific
runtime image. Each production deployment remains responsible for building and validating its own
pinned toolchain image.
