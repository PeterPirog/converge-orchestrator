# Referencja konfiguracji `converge.yaml`

`converge.yaml` jest jedynym plikiem, który powinien być ręcznie edytowany dla konkretnego projektu.
Converge oddziela konfigurację użytkownika od wygenerowanego runtime configu OpenCode i od durable
state procesu.

Kanoniczny, w pełni komentowany wzorzec: [`examples/converge.yaml`](../examples/converge.yaml).
Hardened execution boundary: [`EXECUTION_SANDBOX.md`](EXECUTION_SANDBOX.md).

## Zasady nadrzędne

1. `converge.yaml` trzymaj poza repozytorium rozwijanym autonomicznie.
2. Nie zapisuj w nim sekretów; używaj nazw zmiennych środowiskowych.
3. `architecture.md` trzymaj poza target repo i ustaw read-only.
4. Zmiana projektu ma wymagać zmiany YAML, nie grafu LangGraph.
5. Jawne deterministic quality gates/verifiers są ważniejsze niż heurystyki LLM.
6. Generated OpenCode config jest artefaktem runtime, nie Source of Truth.
7. Project YAML może wybierać modele i custom/MCP tools, ale nie może osłabiać twardych granic ról.
8. Builder jest jedynym writerem; parallelism jest domyślnie ograniczony do read-only review.
9. W autonomous/untrusted mode używaj container sandboxu; permissions modelu nie zastępują granicy OS.

## Top-level

```yaml
version: 1
project: {}
github: {}
opencode: {}
sandbox: {}
models: {}
agents: {}
quality: {}
workflow: {}
```

`version` musi obecnie wynosić `1`. Wcześniejszy płaski format jest nadal akceptowany dla
kompatybilności wstecznej, ale nowe projekty powinny używać formatu sekcyjnego.

Ścieżki względne są rozwiązywane względem katalogu `converge.yaml`, nie względem bieżącego working
directory procesu/PyCharm.

---

## `project`

```yaml
project:
  name: payments
  repo_path: ./repository
  requirements_path: ./architecture.md
  state_dir: null
  worktree_dir: null
  require_spec_read_only: true
```

- `name`: opcjonalna nazwa czytelna dla człowieka.
- `repo_path`: lokalny klon target repo. Bazowy checkout nie jest miejscem pracy Buildera.
- `requirements_path`: immutable Markdown z architekturą/wymaganiami, poza `repo_path`.
- `state_dir`: `null` => `<repo-parent>/.converge`; checkpointy, contract, compliance i evidence.
- `worktree_dir`: `null` => `<state_dir>/worktrees`.
- `require_spec_read_only`: domyślnie `true`; w autonomous mode nie zaleca się wyłączania.

Converge dodatkowo pinuje SHA-256 requirements i zatrzymuje run, jeżeli Source of Truth zmieni się w
trakcie pracy.

---

## `github`

```yaml
github:
  repo: acme/payments
  cli: gh
  base_branch: main
  branch_prefix: converge/
  auto_merge: false
  merge_method: squash
  ci_poll_seconds: 15
  ci_timeout_seconds: 1800
```

- `repo`: `owner/name`; `null` wyłącza GitHub PR/CI.
- `cli`: hostowa binarka GitHub CLI, domyślnie `gh`.
- `base_branch`: canonical base, domyślnie `main`.
- `branch_prefix`: domyślnie `converge/`.
- `auto_merge`: merge dopiero po local gates, review, policy i remote CI.
- `merge_method`: `merge`, `squash` albo `rebase`.
- `ci_poll_seconds`: częstotliwość obserwacji CI.
- `ci_timeout_seconds`: twardy timeout CI observation.

Builder nie dostaje `gh` ani `git push`; finalna integracja jest deterministyczną warstwą hostowego
orkiestratora.

---

## `opencode`

```yaml
opencode:
  binary: opencode
  attach_url: null
  auto_approve: true
  generated_config_path: null
  mcp:
    servers: {}
```

### `binary`

Nazwa lub ścieżka do stable OpenCode CLI. W `sandbox.mode: host` musi istnieć na hoście. W
`sandbox.mode: container` musi istnieć wewnątrz skonfigurowanego obrazu runtime.

### `attach_url`

W host mode może wskazywać persistent OpenCode server, np. `http://127.0.0.1:4096`.

W container mode `attach_url` jest **zabroniony fail-closed**. Attached server wykonywałby narzędzia
poza procesem kontrolowanym przez `ExecutionSandbox`, czyli omijałby granicę bezpieczeństwa.

### `auto_approve`

Automatyzuje operacje OpenCode sklasyfikowane jako `ask`, ale jawne `deny` generowane przez Converge
nadal obowiązują. Nie jest to zamiennik sandboxu.

### `generated_config_path`

`null` => `<state_dir>/opencode.generated.json`. Plik jest generowany i nadpisywany. Dla agent calls
Converge przekazuje także high-precedence inline runtime config, żeby settings z target repo nie mogły
osłabić role boundaries.

### `mcp`

Neutralny user-facing format remote MCP:

```yaml
opencode:
  mcp:
    servers:
      docs:
        type: remote
        url: https://mcp.example.com/mcp
        enabled: true
        oauth: false
        headers:
          X-API-Key: "{env:DOCS_MCP_API_KEY}"
```

Local MCP:

```yaml
opencode:
  mcp:
    servers:
      local-tools:
        type: local
        command: [python, -m, my_mcp_server]
        enabled: true
        environment:
          API_KEY: "{env:LOCAL_MCP_API_KEY}"
```

Converge konwertuje `servers` do stable OpenCode MCP config i nie interpoluje sekretów. MCP jest
rozszerzeniem narzędzi, nie finalną authority: commit/push/PR/merge, requirement hash i deterministic
gates pozostają po stronie orkiestratora.

---

## `sandbox`

Domyślny compatibility profile:

```yaml
sandbox:
  mode: host
  engine: docker
  image: null
  agent_network: none
  quality_network: none
  agent_gateway_base_url: null
  require_internal_agent_network: true
  read_only_root: true
  pids_limit: 512
  memory: 8g
  cpus: 4.0
  tmpfs_size: 2g
  pass_env: []
  user: host
```

Hardened autonomous profile:

```yaml
sandbox:
  mode: container
  engine: docker
  image: ghcr.io/acme/payments-converge-runtime@sha256:...
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
```

### `mode`

- `host`: procesy są uruchamiane bez OS/container boundary; zachowane dla kompatybilności.
- `container`: OpenCode, quality gates i requirement verifiers przechodzą przez wspólny
  `ExecutionSandbox`.

### `engine` / `image`

`engine` jest hostową binarką runtime, obecnie profil jest projektowany pod Docker CLI.

`image` jest wymagany w container mode. Converge używa `--pull=never`: obraz musi istnieć lokalnie i
powinien być przygotowany/pinowany przez deployment projektu. Musi zawierać OpenCode oraz toolchain
potrzebny target repo do build/test/lint/typecheck/verifiers. Local MCP executables także muszą być w
obrazie.

### `agent_network`

Jeżeli `require_internal_agent_network: true`, musi wskazywać nazwaną Docker network z `Internal=true`.
`none` i `host` są odrzucane. `converge doctor` sprawdza faktyczną flagę sieci, nie tylko nazwę.

Przykład provisioningu:

```bash
docker network create --internal converge-ai
```

Converge nie tworzy sieci automatycznie.

### `quality_network`

Sieć dla deterministic quality gates i requirement verifiers. Hardened default to `none`. Jeżeli
integration tests wymagają sieci, skonfiguruj dedykowaną sieć o minimalnym zasięgu zamiast `host`.

### `agent_gateway_base_url`

Hostowy `models.gateway.base_url` może być np. `http://127.0.0.1:3000/api`, ale ten adres nie oznacza
tego samego hosta z perspektywy kontenera. `agent_gateway_base_url` podaje endpoint OpenWebUI/model
gateway widoczny z `agent_network`, np. `http://open-webui:8080/api`.

W container mode generated OpenCode provider używa tego override. Loopback runtime gateway jest
odrzucany fail-closed.

### Filesystem/process policy

Container runner stosuje m.in. read-only root, `cap-drop=ALL`, `no-new-privileges`, PID/RAM/CPU limit,
tmpfs scratch i unikalną nazwę kontenera. Timeout uruchamia `docker rm -f` tej instancji.

Scout/Planner/Reviewers dostają repo/worktree read-only. Builder dostaje RW tylko active worktree,
podczas gdy shared `.git` i `worktree/.git` pointer są osobno read-only. Commit/push/PR/merge nie są
wykonywane przez sandboxowanego agenta.

### `pass_env`

Lista dodatkowych **nazw** ENV do przekazania. Gateway API key oraz `{env:NAME}` występujące w gateway
headers/MCP są wykrywane automatycznie. Converge nie eksportuje całego host environment wildcardem.

### `user`

- `host`: na POSIX używa UID/GID operatora dla bind mountów.
- `image`: używa usera z obrazu; wymaga świadomie przygotowanych permissions.

Pełny threat/trust model i procedura preflight: [`EXECUTION_SANDBOX.md`](EXECUTION_SANDBOX.md).

---

## `models`

### OpenWebUI gateway

```yaml
models:
  gateway:
    kind: openwebui
    provider_id: openwebui
    name: OpenWebUI
    base_url: http://127.0.0.1:3000/api
    api_key_env: OPENWEBUI_API_KEY
```

`base_url` jest endpointem widocznym przez hostowy Converge (`models`, `doctor`). W container mode
agent-visible endpoint konfiguruje `sandbox.agent_gateway_base_url`.

Wartość API key nie trafia do YAML ani generated JSON — zapisuje się tylko nazwa ENV.

### Generic OpenAI-compatible gateway

```yaml
models:
  gateway:
    kind: openai_compatible
    provider_id: internal-llm
    name: Internal LLM Gateway
    base_url: https://llm.example.com/v1
    api_key_env: INTERNAL_LLM_API_KEY
```

### Existing OpenCode provider

```yaml
models:
  gateway:
    kind: existing
  profiles:
    planner:
      model: anthropic/example-model
```

Dla `kind: existing` podaj pełne `provider/model` albo jawny `provider` w profilu.

### Model profiles

Przykład:

```yaml
models:
  profiles:
    planner:
      model: deepseek-v4-pro:cloud
      context_tokens: 1048576
      request_body: {}
    builder:
      model: kimi-k2.7-code:cloud
      context_tokens: 262144
      request_body: {}
```

Pola:

- `model`: dokładne ID zwracane przez gateway.
- `provider`: opcjonalny provider override.
- `name`: czytelna nazwa w generated catalog.
- `variant`: opcjonalny wariant provider-specific.
- `context_tokens`: jawny limit context; trafia do OpenCode `limit.context` i budżetu Converge.
- `output_tokens`: opcjonalny jawny limit output.
- `request_body`: model/provider-specific options; nie może nadpisać safety fields agenta.

Routing referencyjny jest heterogeniczny celowo. Szczegóły: [`MODEL_ROUTING.md`](MODEL_ROUTING.md).

Lista modeli widocznych przez hostowy gateway:

```bash
converge models --config /path/to/converge.yaml
```

---

## `agents`

Referencyjny preset:

```yaml
agents:
  scout:
    agent: converge-scout
    model_profile: scout
    timeout_seconds: 900
    steps: 12
    tool_permissions: {}

  planner:
    agent: converge-planner
    model_profile: planner
    timeout_seconds: 1800
    steps: 18
    tool_permissions: {}

  builder:
    agent: converge-builder
    model_profile: builder
    timeout_seconds: 3600
    steps: 60
    tool_permissions: {}

  correctness_reviewer:
    agent: converge-correctness-reviewer
    model_profile: reviewer
    timeout_seconds: 1800
    steps: 24
    tool_permissions: {}

  architecture_reviewer:
    agent: converge-architecture-reviewer
    model_profile: planner
    timeout_seconds: 1800
    steps: 24
    tool_permissions: {}

  security_reviewer:
    agent: converge-security-reviewer
    model_profile: security
    timeout_seconds: 1800
    steps: 24
    tool_permissions: {}
```

Starsze konfiguracje mogą definiować pojedynczy `reviewer` i pominąć `workflow.review_roles`.

Pola agenta:

- `agent`: unikalny stable OpenCode runtime agent ID.
- `model_profile`: nazwa z `models.profiles`.
- `model`: alternatywa dla profilu; nie ustawiaj jednocześnie obu.
- `timeout_seconds`: twardy timeout pojedynczego agent call.
- `steps`: maksymalny tool/model loop budget.
- `request_body`: per-agent override profilu, bez chronionych safety fields.
- `tool_permissions`: wyłącznie exact custom/MCP tool overrides.

Twarde role:

- Scout/Planner/Reviewers: read-only, bez edit, bez external directory/nested task delegation;
- Builder: edit + lokalny shell w worktree, ale bez `git push`, `gh`, destructive reset/clean,
  external directory i nested task delegation.

Project YAML nie może nadpisać chronionych permissions takich jak `edit`, `bash`, `task`,
`external_directory`, `read` czy `websearch`.

---

## `quality`

```yaml
quality:
  auto_discover: true
  gates: []
  requirement_verifiers: {}
```

`auto_discover` konserwatywnie rozpoznaje Python, Node, Go i Rust z manifestów/metadanych.

Jawny gate:

```yaml
quality:
  gates:
    - name: project-test-suite
      command: [make, test]
      required: true
      timeout_seconds: 1800
```

Exit code jest źródłem prawdy. Brak narzędzia i timeout są failure, nie opinią modelu.

Requirement-specific verifier:

```yaml
quality:
  requirement_verifiers:
    ARCH-017:
      - name: architecture-boundary
        command: [pytest, -q, tests/architecture/test_boundary.py]
        required: true
        timeout_seconds: 600
```

Reguły monotonic convergence:

- istniejący baseline `FAIL` może pozostać `FAIL` w unrelated change;
- mandatory `PASS` nie może przejść do non-PASS;
- target z configured verifier musi wykazać rzeczywisty postęp do `PASS` przed integracją.

Requirement IDs bierz z `<state_dir>/contract.json`.

W active graph repo-controlled quality commands i requirement verifiers są wykonywane **przed** finalnym
scope/diff gate, aby generated files nie mogły pojawić się po zaakceptowaniu zakresu.

---

## `workflow`

```yaml
workflow:
  max_repair_attempts: 3
  max_replans: 2
  max_iterations: 50
  max_diff_lines_hard: 1000
  context_input_fraction: 0.70
  context_output_reserve_tokens: 4096
  review_roles:
    - correctness_reviewer
    - architecture_reviewer
    - security_reviewer
  max_parallel_reviews: 3
```

Budżety autonomii są skończone. Nie konfiguruj nieskończonych retry/replan loops.

`context_input_fraction` i `context_output_reserve_tokens` tworzą fail-closed input budget. Authoritative
requirements/task/review diff nie są automatycznie obcinane; tylko jawnie advisory Scout/working
memory może być kompaktowane.

`review_roles` jest listą wymaganych read-only lanes. Jeden `reject`, malformed JSON, timeout lub
failure procesu daje aggregate `reject`. Brak listy zachowuje compatibility mode z pojedynczym
`reviewer`.

`max_parallel_reviews` ma zakres 1–16 i ogranicza concurrency, nie liczbę wymaganych PASS. Jeżeli
sprzęt nie utrzymuje ciężkich modeli równocześnie, zmniejsz concurrency zamiast usuwać reviewerów.

---

## Generated `opencode.generated.json`

Converge tworzy provider/model catalog, kompletne agent definitions, permissions i MCP. Sekretów nie
serializuje do generated JSON. W container mode provider `baseURL` może zostać zastąpiony przez
`sandbox.agent_gateway_base_url`, ale hostowy Source of Truth konfiguracji pozostaje w
`converge.yaml`.

---

## Walidacja konfiguracji

Lista modeli:

```bash
converge models --config /path/to/converge.yaml
```

Pełny preflight:

```bash
converge doctor --config /path/to/converge.yaml
```

`doctor` sprawdza ścieżki, read-only Source of Truth, requirement IDs, stack/gates, model routing i
live model catalog. W container mode dodatkowo sprawdza engine, lokalny image, network existence,
`Internal=true` agent network oraz runtime policies takie jak zakaz attach servera/loopback gateway.
Nie robi implicit pull ani network provisioning.

Offline:

```bash
converge doctor --offline --config /path/to/converge.yaml
```

Pomija live model catalog check; nie omija local sandbox/spec/config validation.

## Co zmieniać przy nowym projekcie

Najczęściej:

```text
project.name
project.repo_path
project.requirements_path
github.repo
models.gateway.*
models.profiles.*
sandbox.*             # jeśli używasz hardened container mode
quality.gates
quality.requirement_verifiers
```

Nie zmieniaj grafu tylko dlatego, że target repo używa innego języka, modelu, MCP albo obrazu runtime.
