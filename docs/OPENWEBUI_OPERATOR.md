# OpenWebUI operator bridge

Converge keeps **LangGraph as the durable workflow engine**. OpenWebUI is an operator surface and
model gateway; it does not own checkpoints, repair/replan state, compliance, Git integration or merge
policy.

The supported operator integration is the native Workspace Tool in:

```text
integrations/openwebui/converge_operator.py
```

It calls the existing Converge FastAPI control plane. This deliberately avoids making legacy
OpenWebUI Pipelines part of the runtime architecture.

## Security model

There are two independent controls around operator mutations:

1. The Converge API can require a bearer token through `CONVERGE_API_TOKEN`.
2. The OpenWebUI Workspace Tool requires an interactive browser confirmation before every mutating
   operation.

Read-only operations do not ask for confirmation. Mutating operations fail closed when the browser
cannot answer, the confirmation times out/disconnects or the operator chooses Cancel.

The Workspace Tool can:

- read registered projects, run state, compliance, evidence and pending interrupts;
- register/bootstrap a project after confirmation;
- start an autonomous LangGraph run after confirmation;
- request cooperative pause/resume after confirmation;
- submit a HITL decision after first reading the pending interrupt and showing it to the operator.

The Tool cannot waive deterministic quality gates, review rejection, Source-of-Truth hash checks or CI.
Those rules remain inside Converge/LangGraph.

## Start the authenticated Converge API

Generate a high-entropy token and keep it outside Git/YAML:

```bash
export CONVERGE_API_TOKEN="$(python -c 'import secrets; print(secrets.token_urlsafe(32))')"
export CONVERGE_API_HOST=0.0.0.0
export CONVERGE_API_PORT=8088
converge-api
```

`/health` remains unauthenticated for liveness probes. Every other endpoint requires:

```text
Authorization: Bearer <CONVERGE_API_TOKEN>
```

For a process listening only on trusted localhost, leaving `CONVERGE_API_TOKEN` unset preserves the
legacy local behavior. When OpenWebUI reaches Converge over Docker, LAN or another network namespace,
enable the token. For traffic crossing an untrusted network, terminate TLS at a trusted reverse proxy;
do not expose the bearer token over plaintext HTTP.

## Install the Workspace Tool

Workspace Tools execute Python inside the OpenWebUI process and therefore must be treated as trusted
server code. Review this repository's Tool source before importing it and restrict Workspace Tool
creation/import permissions to trusted administrators.

In OpenWebUI:

1. Open **Workspace > Tools**.
2. Use **Create** and paste the contents of `integrations/openwebui/converge_operator.py`, or use the
   Tool import mechanism available in your deployment.
3. Save the Tool.
4. Open its Valves and configure:
   - `base_url` — URL reachable **from the OpenWebUI server/container**;
   - `api_token` — the same value as `CONVERGE_API_TOKEN`;
   - `timeout_seconds` — request timeout for control-plane calls.
5. Grant read/use access only to trusted operators or to a dedicated operator model.
6. Enable the Tool in the operator chat/model.

Common `base_url` examples:

```text
# OpenWebUI and Converge on the same host without container isolation
http://127.0.0.1:8088

# Docker Desktop: OpenWebUI container -> host Converge process
http://host.docker.internal:8088

# Docker Compose service-to-service networking
http://converge:8088
```

Use the address that is resolvable from the OpenWebUI backend, not necessarily from your browser.

## Operator workflow

A typical project flow from chat is:

```text
register_project
    -> explicit confirmation
bootstrap_project
    -> explicit confirmation
project_compliance                  [read-only]
start_project
    -> explicit confirmation
run_status                          [read-only]
       |
       +--> running / repair / CI
       |
       +--> interrupted
              -> pending_interrupt  [read-only]
              -> decide_run
                    -> explicit confirmation with interrupt evidence
```

`pause_run` requests a cooperative stop at a safe LangGraph boundary. `resume_run` continues from the
same durable checkpoint; it does not reconstruct state from chat history.

## Confirmation semantics

Every mutating Tool method receives OpenWebUI's `__event_call__` helper and emits a native
`confirmation` event. The code accepts only an explicit positive response. Missing event support,
Cancel, browser disconnect and event-call error all produce a cancelled result **before** any mutating
HTTP request is sent.

HITL decisions have an additional guard: the Tool first performs a read-only request for the current
interrupt, displays that evidence and requested action in the confirmation dialog, and only then may
POST the decision.

This is an operator safety boundary, not a replacement for Converge policy. A confirmed operation can
start or resume work, but cannot convert a failing deterministic gate into PASS.

## Native Tool versus direct OpenAPI connection

The Converge FastAPI service exposes OpenAPI and can also be connected directly as an OpenAPI Tool
Server. That is useful for read-only exploration and integrations. The native Workspace Tool is the
recommended operator surface for mutating controls because OpenWebUI supports bidirectional interactive
confirmation through `__event_call__` only for native Python Tools/Functions.

Current OpenWebUI references:

- Workspace Tools: <https://docs.openwebui.com/features/extensibility/plugin/tools/>
- Tool development and optional arguments: <https://docs.openwebui.com/features/extensibility/plugin/tools/development/>
- Interactive events: <https://docs.openwebui.com/features/extensibility/plugin/development/events/>
- OpenAPI Tool Servers: <https://docs.openwebui.com/features/extensibility/plugin/tools/openapi-servers/>

## Durable-state invariant

The following must remain true as the UI evolves:

```text
OpenWebUI chat / Workspace Tool
            |
            | authenticated, confirmed control requests
            v
      Converge FastAPI
            |
            v
      LangGraph + SQLite
            |
       durable state
```

Do not persist workflow authority in OpenWebUI chat memory, Tool globals or browser state. Those layers
may display or request operations; LangGraph remains authoritative.
